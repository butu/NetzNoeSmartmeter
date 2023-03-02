"""
WienerNetze Smartmeter sensor platform
"""
import collections
import logging
from decimal import Decimal
from datetime import timedelta, datetime, timezone
from typing import Any, Callable, Dict, Optional

import voluptuous as vol
from homeassistant import core, config_entries

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    ENTITY_ID_FORMAT,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DEVICE_ID,
    ENERGY_KILO_WATT_HOUR,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    async_import_statistics,
)
from homeassistant.core import DOMAIN
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
    HomeAssistantType,
)
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import Smartmeter
from .const import (
    ATTRS_WELCOME_CALL,
    ATTRS_ZAEHLPUNKTE_CALL,
    ATTRS_VERBRAUCH_CALL,
    CONF_ZAEHLPUNKTE,
)
from .utils import before, today, translate_dict

_LOGGER = logging.getLogger(__name__)
# Time between updating data from Wiener Netze
SCAN_INTERVAL = timedelta(minutes=15)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_DEVICE_ID): cv.string,
    }
)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    """Setup sensors from a config entry created in the integrations UI."""
    config = hass.data[DOMAIN][config_entry.entry_id]
    sensors = [
        SmartmeterSensor(
            config[CONF_USERNAME], config[CONF_PASSWORD], zp["zaehlpunktnummer"]
        )
        for zp in config[CONF_ZAEHLPUNKTE]
    ]
    async_add_entities(sensors, update_before_add=True)


async def async_setup_platform(
    hass: HomeAssistantType,  # pylint: disable=unused-argument
    config: ConfigType,
    async_add_entities: collections.abc.Callable,
    discovery_info: Optional[
        DiscoveryInfoType
    ] = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the sensor platform by adding it into configuration.yaml"""
    sensor = SmartmeterSensor(
        config[CONF_USERNAME], config[CONF_PASSWORD], config[CONF_DEVICE_ID]
    )
    async_add_entities([sensor], update_before_add=True)


class SmartmeterSensor(SensorEntity):
    """
    Representation of a Wiener Smartmeter sensor
    for measuring total increasing energy consumption for a specific zaehlpunkt
    """

    def __init__(self, username: str, password: str, zaehlpunkt: str) -> None:
        super().__init__()
        self.username = username
        self.password = password
        self.zaehlpunkt = zaehlpunkt

        self._attr_native_value = int
        self._attr_extra_state_attributes = {}
        self._attr_name = zaehlpunkt
        self._attr_icon = "mdi:flash"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = ENERGY_KILO_WATT_HOUR
        self._attr_unit_of_measurement = ENERGY_KILO_WATT_HOUR

        self.attrs: dict[str, Any] = {}
        self._name: str = zaehlpunkt
        self._state: int = None
        self._available: bool = True
        self._updatets: str = None

        self._id = ENTITY_ID_FORMAT.format(slugify(self._name).lower())

    @property
    def icon(self) -> str:
        """
        Return icon
        """
        return self._attr_icon

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        if "label" in self._attr_extra_state_attributes:
            return self._attr_extra_state_attributes["label"]
        return self._name

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.zaehlpunkt

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    @property
    def state(self) -> Optional[str]:  # pylint: disable=overridden-final-method
        return self._state

    async def get_zaehlpunkt(self, smartmeter: Smartmeter) -> dict[str, str]:
        """
        asynchronously get and parse /zaehlpunkt response
        Returns response already sanitzied of the specified zahlpunkt in ctor
        """
        zps = await self.hass.async_add_executor_job(smartmeter.zaehlpunkte)
        if "Exception" in zps:
            raise RuntimeError(f"Cannot access zählpunkte: {zps}")

        if zps is None or len(zps) == 0:
            raise RuntimeError(f"Cannot access Zaehlpunkt {self.zaehlpunkt}")

        zp = [
            z
            for z in zps[0]["zaehlpunkte"]
            if z["zaehlpunktnummer"] == self.zaehlpunkt
        ]
        if len(zp) == 0:
            raise RuntimeError(f"Zaehlpunkt {self.zaehlpunkt} not found")

        return (
            translate_dict(zp[0], ATTRS_ZAEHLPUNKTE_CALL)
            if len(zp) > 0
            else None
        )

    async def get_consumption(self, smartmeter: Smartmeter, start_date: datetime):
        """Return 24h of hourly consumption starting from a date"""
        response = await self.hass.async_add_executor_job(
            smartmeter.verbrauch, start_date, self.zaehlpunkt
            )
        if "Exception" in response:
            raise RuntimeError(f"Cannot access daily consumption: {response}")

        return translate_dict(response, ATTRS_VERBRAUCH_CALL)

    async def get_welcome(self, smartmeter: Smartmeter) -> dict[str, str]:
        """
        asynchronously get adn parse /welcome response
        Returns response already sanitzied of the specified zahlpunkt in ctor
        """
        response = await self.hass.async_add_executor_job(smartmeter.welcome)
        if "Exception" in response:
            raise RuntimeError(f"Cannot access welcome: {response}")

        return translate_dict(response, ATTRS_WELCOME_CALL)

    async def _import_statistics(self, smartmeter: Smartmeter, start: datetime, sum_: Decimal):
        """Import hourly consumption data into the statistics module, using start date and sum"""
        # Have to be sure that the start datetime is aware of timezone, because we need to compare
        # it to other timezone aware datetimes in this function
        if start.tzinfo is None:
            raise ValueError("start datetime must be timezone-aware!")
        # Have to be sure that full minutes are used. otherwise, the API returns a different
        # interval
        start = start.replace(minute=0, second=0, microsecond=0)

        statistics = []
        metadata = StatisticMetaData(
            source="recorder",
            statistic_id=self._id,
            name=self.name,
            unit_of_measurement=self._attr_unit_of_measurement,
            has_mean=False,
            has_sum=True,
        )
        _LOGGER.debug(metadata)

        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        _LOGGER.debug(f"Selecting data up to {now}")
        while start < now:
            _LOGGER.debug(f"Select 24h of Data, using sum={sum_:.3f}, start={start}")
            verbrauch = await self.get_consumption(smartmeter, start)
            _LOGGER.debug(verbrauch)
            last_ts = start
            start += timedelta(hours=24)  # Next batch. Setting this here should avoid endless loops

            if 'values' not in verbrauch:
                _LOGGER.error(f"No values in API response! This likely indicates an API error. Original response: {verbrauch}")
                return

            # Check if this batch of data is valid and contains hourly statistics:
            if not verbrauch.get('optIn'):
                # TODO: actually, we could insert zero-usage data here, to increase the start time
                # for the next run. Otherwise, the same data is queried over and over.
                _LOGGER.warning(f"Data starting at {start} does not contain granular data! Opt-in was not set back then.")
                continue

            # Can actually check, if the whole batch can be skipped.
            if verbrauch.get('consumptionMinimum') == 0 and verbrauch.get('consumptionMaximum') == 0:
                _LOGGER.debug("Batch of data does not contain any consumption, skipping")
                continue

            for v in verbrauch['values']:
                # Timestamp has to be aware of timezone, parse_datetime does that.
                ts = dt_util.parse_datetime(v['timestamp'])
                if ts.minute != 0:
                    # This usually happens if the start date minutes are != 0
                    # However, we set them to 0 in this function, thus if this happens, the API has
                    # a problem...
                    _LOGGER.error("Minute of timestamp is non-zero, this must not happen!")
                    return
                if ts < last_ts:
                    # TODO: What happens on summer-/wintertime change in the statistics?
                    # This should prevent any issues with ambigous values though...
                    _LOGGER.warning(f"Timestamp from API ({ts}) is less than previously collected timestamp ({last_ts}), ignoring value!")
                    continue
                last_ts = ts
                if v['value'] is None:
                    # Usually this means that the measurement is not yet in the WSTW database.
                    # But could also be an error? Dunno...
                    # For now we ignore these values, possibly that means we loose hours if these
                    # values come back later.
                    # However, it is not trivial (or even impossible?) to insert statistic values
                    # in between existing values, thus we can not do much.
                    continue
                usage = Decimal(v['value'] / 1000.0)  # Convert to kWh ...
                sum_ += usage  # ... and accumulate
                if v['isEstimated']:
                    # Can we do anything special here?
                    _LOGGER.debug("Estimated Value found for {ts}: {usage}")

                statistics.append(StatisticData(start=ts, sum=sum_, state=usage))

        _LOGGER.debug(statistics)

        # Import the statistics data
        async_import_statistics(self.hass, metadata, statistics)

    def is_active(self, zaehlpunkt_response: dict) -> bool:
        """
        returns active status of smartmeter, according to zaehlpunkt response
        """
        return (
            not ("active" in zaehlpunkt_response) or zaehlpunkt_response["active"]
        ) or (
            not ("smartMeterReady" in zaehlpunkt_response)
            or zaehlpunkt_response["smartMeterReady"]
        )

    async def async_update(self):
        """
        update sensor
        """
        self._available = True

        # Query the statistics database for the last value
        # It is crucial to use get_instance here!
        last_inserted_stat = await get_instance(
            self.hass
        ).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,  # Get at most one entry
            self._id,  # of this sensor
            True,  # convert the units
            # XXX: since HA core 2022.12 need to specify this:
            {"sum", "state"},  # the fields we want to query (state might be used in the future)
        )
        _LOGGER.debug(f"Last inserted stat: {last_inserted_stat}")

        if len(last_inserted_stat) == 0 or len(last_inserted_stat[self._id]) == 0:
            # No previous data - start from scratch
            _sum = Decimal(0)
            # Select as start date two days before the current day.
            # Could be increased to load a lot of historical data, but we do not want to
            # strain the API...
            start = today(timezone.utc) - timedelta(hours=48)
        elif len(last_inserted_stat) == 1 and len(last_inserted_stat[self._id]) == 1:
            # Previous data found in the statistics table
            _sum = Decimal(last_inserted_stat[self._id][0]["sum"])
            # The next start is the previous end
            # XXX: since HA core 2022.12, we get a datetime and not a str...
            # XXX: since HA core 2023.03, we get a float and not a datetime...
            start = last_inserted_stat[self._id][0]["end"]
            if isinstance(start, (int, float)):
                start = dt_util.utc_from_timestamp(start)
            if isinstance(start, str):
                start = dt_util.parse_datetime(start)

            if not isinstance(start, datetime):
                _LOGGER.error("HA core decided to change the return type AGAIN! "
                              "Please open a bug report. "
                              "Additional Information: %s Type: %s",
                              last_inserted_stat,
                              type(last_inserted_stat[self._id][0]["end"]))
                return
            _LOGGER.debug("New starting datetime: %s", start)

            # Extra check to not strain the API too much:
            # If the last insert date is less than 24h away, simply exit here,
            # because we will not get any data from the API
            min_wait = timedelta(hours=24)
            delta_t = datetime.now(timezone.utc).replace(microsecond=0) - start.replace(microsecond=0)
            if delta_t <= min_wait:
                _LOGGER.debug(f"Not querying the API, because last update is not older than 24 hours. Earliest update in {min_wait - delta_t}")
                return

        else:
            _LOGGER.error(f"unexpected result of get_last_statistics: {last_inserted_stat}")
            return

        try:
            smartmeter = Smartmeter(self.username, self.password)
            await self.hass.async_add_executor_job(smartmeter.login)
            zaehlpunkt = await self.get_zaehlpunkt(smartmeter)
            self._attr_extra_state_attributes = zaehlpunkt

            if not self.is_active(zaehlpunkt):
                self._available = False
                _LOGGER.error(f"Smartmeter {zaehlpunkt} is not active!")
                return

            # Collect hourly data
            await self._import_statistics(smartmeter, start, _sum)

            self._updatets = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        except RuntimeError:
            self._available = False
            _LOGGER.exception("Error retrieving data from smart meter api")
