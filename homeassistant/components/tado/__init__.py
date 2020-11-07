"""Support for the (unofficial) Tado API."""
from datetime import timedelta
import logging

from PyTado.interface import Tado
from requests import RequestException
import voluptuous as vol

from homeassistant.components.climate.const import PRESET_AWAY, PRESET_HOME
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import load_platform
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.util import Throttle
from homeassistant.helpers.entity_component import EntityComponent

from .const import CONF_FALLBACK, DATA
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tado"

SIGNAL_TADO_UPDATE_RECEIVED = "tado_update_received_{}_{}"

TADO_COMPONENTS = ["sensor", "climate", "water_heater"]

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=10)
SCAN_INTERVAL = timedelta(seconds=15)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                {
                    vol.Required(CONF_USERNAME): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_FALLBACK, default=True): cv.boolean,
                }
            ],
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(hass, config):
    """Set up of the Tado component."""
    acc_list = config[DOMAIN]

    for conf in config[DOMAIN]:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=conf,
            )
        )

    return True

    for acc in acc_list:
        username = acc[CONF_USERNAME]
        password = acc[CONF_PASSWORD]
        fallback = acc[CONF_FALLBACK]

        tadoconnector = TadoConnector(hass, username, password, fallback)
        if not tadoconnector.setup():
            continue

        # Do first update
        tadoconnector.update()

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    fallback = entry.options.get(CONF_FALLBACK, True)

    tadoconnector = TadoConnector(hass, username, password, fallback)

    try:
        await hass.async_add_executor_job(tadoconnector.setup)
    except KeyError:
        _LOGGER.error("Failed to login to tado")
        return False
    except RuntimeError as exc:
        _LOGGER.error("Failed to setup tado: %s", exc)
        return ConfigEntryNotReady
    except requests.exceptions.Timeout as ex:
        raise ConfigEntryNotReady from ex
    except requests.exceptions.HTTPError as ex:
        if ex.response.status_code > 400 and ex.response.status_code < 500:
            _LOGGER.error("Failed to login to tado: %s", ex)
            return False
        raise ConfigEntryNotReady from ex

    # Do first update
    await hass.async_add_executor_job(tadoconnector.update)

    # Poll for updates in the background
    update_track = async_track_time_interval(
        hass,
        lambda now: tadoconnector.update(),
        SCAN_INTERVAL,
    )

    update_listener = entry.add_update_listener(_async_update_listener)

    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][DATA] = api_data_list

    # Load components
    for component in TADO_COMPONENTS:
        load_platform(
            hass, component, DOMAIN, {}, config,
        )

    return True


class TadoConnector:
    """An object to store the Tado data."""

    def __init__(self, hass, username, password, fallback):
        """Initialize Tado Connector."""
        self.hass = hass
        self._username = username
        self._password = password
        self._fallback = fallback

        self.device_id = None
        self.tado = None
        self.zones = None
        self.devices = None
        self.data = {
            "zone": {},
            "device": {},
        }

    @property
    def fallback(self):
        """Return fallback flag to Smart Schedule."""
        return self._fallback

    def setup(self):
        """Connect to Tado and fetch the zones."""
        try:
            self.tado = Tado(self._username, self._password)
        except (RuntimeError, RequestException) as exc:
            _LOGGER.error("Unable to connect: %s", exc)
            return False

        self.tado.setDebugging(True)

        # Load zones and devices
        self.zones = self.tado.getZones()
        self.devices = self.tado.getMe()["homes"]
        self.device_id = self.devices[0]["id"]
        return True

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Update the registered zones."""
        for zone in self.zones:
            self.update_sensor("zone", zone["id"])
        for device in self.devices:
            self.update_sensor("device", device["id"])

    def update_sensor(self, sensor_type, sensor):
        """Update the internal data from Tado."""
        _LOGGER.debug("Updating %s %s", sensor_type, sensor)
        try:
            if sensor_type == "zone":
                data = self.tado.getZoneState(sensor)
            elif sensor_type == "device":
                devices_data = self.tado.getDevices()
                if not devices_data:
                    _LOGGER.info("There are no devices to setup on this tado account")
                    return

                data = devices_data[0]
            else:
                _LOGGER.debug("Unknown sensor: %s", sensor_type)
                return
        except RuntimeError:
            _LOGGER.error(
                "Unable to connect to Tado while updating %s %s",
                sensor_type,
                sensor,
            )
            return

        self.data[sensor_type][sensor] = data

        _LOGGER.debug("Dispatching update to %s %s: %s", sensor_type, sensor, data)
        dispatcher_send(
            self.hass, SIGNAL_TADO_UPDATE_RECEIVED.format(sensor_type, sensor)
        )

    def get_capabilities(self, zone_id):
        """Return the capabilities of the devices."""
        return self.tado.getCapabilities(zone_id)

    def reset_zone_overlay(self, zone_id):
        """Reset the zone back to the default operation."""
        self.tado.resetZoneOverlay(zone_id)
        self.update_sensor("zone", zone_id)

    def set_presence(
        self,
        presence=PRESET_HOME,
    ):
        """Set the presence to home or away."""
        if presence == PRESET_AWAY:
            self.tado.setAway()
        elif presence == PRESET_HOME:
            self.tado.setHome()

    def set_zone_overlay(
        self,
        zone_id=None,
        overlay_mode=None,
        temperature=None,
        duration=None,
        device_type="HEATING",
        mode=None,
        fan_speed=None,
        swing=None,
    ):
        """Set a zone overlay."""
        _LOGGER.debug(
            "Set overlay for zone %s: overlay_mode=%s, temp=%s, duration=%s, type=%s, mode=%s fan_speed=%s swing=%s",
            zone_id,
            overlay_mode,
            temperature,
            duration,
            device_type,
            mode,
            fan_speed,
            swing,
        )

        try:
            self.tado.setZoneOverlay(
                zone_id,
                overlay_mode,
                temperature,
                duration,
                device_type,
                "ON",
                mode,
                fanSpeed=fan_speed,
                swing=swing,
            )

        except RequestException as exc:
            _LOGGER.error("Could not set zone overlay: %s", exc)

        self.update_sensor("zone", zone_id)

    def set_zone_off(self, zone_id, overlay_mode, device_type="HEATING", duration=None):
        """Set a zone to off."""
        try:
            self.tado.setZoneOverlay(
                zone_id, overlay_mode, None, duration, device_type, "OFF"
            )
        except RequestException as exc:
            _LOGGER.error("Could not set zone overlay: %s", exc)

        self.update_sensor("zone", zone_id)
