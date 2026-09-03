
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import ayla_iot_unofficial
import ayla_iot_unofficial.device

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import color as color_util

from . import NomaIQConfigEntry
from .const import DOMAIN
from .coordinator import NomaIQDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_DEBOUNCE_SECONDS = 0.25
_INTER_PROPERTY_DELAY_SECONDS = 0.05
_DISCRETE_COLOR_TEMP_MAX_STEPS = 8
_DISCRETE_COLOR_TEMP_PRESET_NAMES = {
    5: ("Candlelight", "Warm White", "White", "Cool White", "Daylight"),
    3: ("Warm White", "White", "Daylight"),
}

_POWER_PROPERTY_CANDIDATES = ("light_control", "light_switch", "power_switch", "power")
_BRIGHTNESS_PROPERTY_CANDIDATES = ("light_brightness", "brightness")
_HUE_PROPERTY_CANDIDATES = (
    "light_hue",
    "hue",
    "colour_hue",
    "color_hue",
    "hsv_hue",
)
_SATURATION_PROPERTY_CANDIDATES = (
    "light_saturation",
    "saturation",
    "colour_saturation",
    "color_saturation",
    "hsv_saturation",
)
_COLOR_TEMP_PROPERTY_CANDIDATES = (
    "color_temp",
    "light_color_temp",
    "colour_temp",
    "light_colour_temp",
    "cct",
    "light_cct",
    "white_value",
    "white_color_temp",
    "correlated_color_temperature",
    "warm_white",
    "cool_white",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NomaIQConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Noma IQ Light platform."""
    coordinator: NomaIQDataUpdateCoordinator = entry.runtime_data

    entities: list[NomaIQLightEntity] = []
    for device in coordinator.data:
        device_properties = getattr(device, "properties_full", {})
        if not hasattr(device_properties, "get"):
            _LOGGER.debug("Skipping device %s: no readable property map", device.serial_number)
            continue

        power_property = next(
            (
                candidate
                for candidate in _POWER_PROPERTY_CANDIDATES
                if isinstance(device_properties.get(candidate), Mapping)
            ),
            None,
        )
        if not power_property:
            _LOGGER.debug(
                "Skipping device %s: no supported power property found (available: %s)",
                device.serial_number,
                sorted(device_properties.keys()),
            )
            continue

        entities.append(NomaIQLightEntity(coordinator, device, power_property))

    if entities:
        async_add_entities(entities, update_before_add=False)
        _LOGGER.debug("Added %d NomaIQ light entities", len(entities))
    else:
        _LOGGER.warning(
            "No NomaIQ light entities discovered. Check that the bulb exposes a supported "
            "power property (%s).",
            ", ".join(_POWER_PROPERTY_CANDIDATES),
        )


class NomaIQLightEntity(LightEntity):
    """Representation of a NomaIQ light."""

    def __init__(
        self,
        coordinator: NomaIQDataUpdateCoordinator,
        device: ayla_iot_unofficial.device.Device,
        power_property: str,
    ) -> None:
        """Initialize a NomaIQ light."""
        self.coordinator = coordinator
        self._device = device
        self._power_property = power_property
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF
        light_name = self._safe_get_property_value("light_name")
        self._attr_name = light_name or device.name
        self._attr_unique_id = f"nomaiq_light_{device.serial_number}"
        self._attr_has_entity_name = bool(light_name)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial_number)},
            name=device.name,
        )
        self._brightness_property = self._find_property(
            _BRIGHTNESS_PROPERTY_CANDIDATES,
            fallback_terms=("bright",),
        )
        self._hue_property = self._find_property(
            _HUE_PROPERTY_CANDIDATES,
            fallback_terms=("hue",),
        )
        self._saturation_property = self._find_property(
            _SATURATION_PROPERTY_CANDIDATES,
            fallback_terms=("saturation", "_sat"),
        )
        self._color_temp_property = self._find_property(
            _COLOR_TEMP_PROPERTY_CANDIDATES,
            fallback_terms=(
                "color_temp",
                "colour_temp",
                "temperature",
                "kelvin",
                "mired",
                "cct",
                "white",
                "warm",
                "cool",
            ),
        )
        self._color_temp_uses_mired = self._detect_color_temp_mired_mode()
        self._set_supported_color_modes()
        self._pending_updates: dict[str, int | float] = {}
        self._pending_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        _LOGGER.warning(
            (
                "NomaIQ light %s property mapping: power=%s brightness=%s hue=%s "
                "saturation=%s color_temp=%s"
            ),
            self._device.serial_number,
            self._power_property,
            self._brightness_property,
            self._hue_property,
            self._saturation_property,
            self._color_temp_property,
        )
        self._log_mapped_property_details()

        if self._color_temp_property and not self._color_temp_is_discrete():
            prop_min = self._property_min_value(self._color_temp_property, 153)
            prop_max = self._property_max_value(self._color_temp_property, 500)
            if self._color_temp_uses_mired:
                self._attr_min_color_temp_kelvin = color_util.color_temperature_mired_to_kelvin(
                    int(prop_max)
                )
                self._attr_max_color_temp_kelvin = color_util.color_temperature_mired_to_kelvin(
                    int(prop_min)
                )
            else:
                low = int(min(prop_min, prop_max))
                high = int(max(prop_min, prop_max))
                self._attr_min_color_temp_kelvin = low
                self._attr_max_color_temp_kelvin = high

    def _set_supported_color_modes(self) -> None:
        supported_modes: set[ColorMode] = set()
        self._attr_supported_features = LightEntityFeature(0)
        if self._hue_property and self._saturation_property:
            supported_modes.add(ColorMode.HS)
        if self._color_temp_property and not self._color_temp_is_discrete():
            supported_modes.add(ColorMode.COLOR_TEMP)
        if not supported_modes and self._brightness_property:
            supported_modes.add(ColorMode.BRIGHTNESS)
        if not supported_modes:
            supported_modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = supported_modes
        if self._color_temp_property and self._color_temp_is_discrete():
            self._attr_supported_features |= LightEntityFeature.EFFECT
            self._attr_effect_list = self._discrete_preset_names()
        if ColorMode.HS in supported_modes:
            self._attr_color_mode = ColorMode.HS
        elif ColorMode.COLOR_TEMP in supported_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.BRIGHTNESS in supported_modes:
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_color_mode = ColorMode.ONOFF

    def _get_current_device(self) -> ayla_iot_unofficial.device.Device | None:
        """Get the current device from coordinator data."""
        data: list[ayla_iot_unofficial.device.Device] = self.coordinator.data
        return next(
            (d for d in data if d.serial_number == self._device.serial_number),
            None,
        )

    def _find_property(
        self,
        candidates: tuple[str, ...],
        *,
        fallback_terms: tuple[str, ...] = (),
    ) -> str | None:
        """Return first known property that exists on this device."""
        properties = getattr(self._device, "properties_full", {})
        if not hasattr(properties, "get"):
            return None
        for candidate in candidates:
            prop = properties.get(candidate)
            if isinstance(prop, Mapping) and prop:
                return candidate
        if fallback_terms:
            for property_name, prop in properties.items():
                if not isinstance(prop, Mapping) or not prop:
                    continue
                normalized_name = property_name.lower()
                if any(term in normalized_name for term in fallback_terms):
                    return property_name
        return None

    def _property_log_details(self, property_name: str | None) -> str:
        """Return compact details for a mapped property for diagnostics."""
        if not property_name:
            return "none"
        prop = self._property_definition(property_name, use_current_device=False)
        if not prop:
            return f"{property_name} (unavailable)"
        api_name = prop.get("name")
        base_type = prop.get("base_type")
        read_only = prop.get("read_only")
        current_value = prop.get("value")
        raw_min = prop.get("min", prop.get("minimum", prop.get("lower")))
        raw_max = prop.get("max", prop.get("maximum", prop.get("upper")))
        return (
            f"{property_name}(api={api_name},type={base_type},ro={read_only},"
            f"value={current_value},min={raw_min},max={raw_max})"
        )

    def _log_mapped_property_details(self) -> None:
        """Log concrete mapped-property metadata for troubleshooting."""
        _LOGGER.warning(
            (
                "NomaIQ light %s mapped details: power=%s brightness=%s hue=%s "
                "saturation=%s color_temp=%s"
            ),
            self._device.serial_number,
            self._property_log_details(self._power_property),
            self._property_log_details(self._brightness_property),
            self._property_log_details(self._hue_property),
            self._property_log_details(self._saturation_property),
            self._property_log_details(self._color_temp_property),
        )

    def _property_definition(
        self,
        property_name: str,
        *,
        use_current_device: bool = True,
    ) -> Mapping[str, Any] | None:
        """Get property metadata, if available."""
        device = self._get_current_device() if use_current_device else self._device
        if not device:
            return None
        properties = getattr(device, "properties_full", {})
        if not hasattr(properties, "get"):
            return None
        prop = properties.get(property_name)
        return prop if isinstance(prop, Mapping) else None

    def _safe_get_property_value(self, property_name: str) -> Any | None:
        """Read property safely from current coordinator state."""
        device = self._get_current_device()
        if not device:
            return None
        if not self._property_definition(property_name):
            return None
        try:
            return device.get_property_value(property_name)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_property_name(property_name: str) -> str:
        """Normalize Ayla GET_/SET_ prefixed property names."""
        normalized = property_name.lower()
        if normalized.startswith("set_") or normalized.startswith("get_"):
            return normalized[4:]
        return normalized

    def _find_set_property_api_name(self, property_name: str) -> str | None:
        """Find API property name for writable SET_* aliases."""
        properties = getattr(self._device, "properties_full", {})
        if not hasattr(properties, "items"):
            return None
        target = self._normalize_property_name(property_name)

        for key, prop in properties.items():
            if not isinstance(prop, Mapping):
                continue
            candidate_name = str(prop.get("name") or key)
            normalized_candidate = candidate_name.lower()
            if not normalized_candidate.startswith("set_"):
                continue
            if self._normalize_property_name(candidate_name) == target:
                return candidate_name

        return None

    def _resolve_write_property_api_name(
        self, property_name: str, prop: Mapping[str, Any]
    ) -> str | None:
        """Resolve the best API property name for writes."""
        set_alias = self._find_set_property_api_name(property_name)
        if set_alias:
            return set_alias
        api_property_name = prop.get("name")
        return str(api_property_name) if api_property_name else None

    @staticmethod
    def _coerce_is_on(value: Any) -> bool:
        """Convert variable API value types to on/off state reliably."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(int(value))
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"0", "off", "false", "no"}:
                return False
            if normalized in {"1", "on", "true", "yes"}:
                return True
        return bool(value)

    @staticmethod
    def _clamp(value: float, min_value: float, max_value: float) -> float:
        """Clamp value to a range."""
        if value < min_value:
            return min_value
        if value > max_value:
            return max_value
        return value

    @classmethod
    def _scale_value(
        cls,
        value: float,
        *,
        source_min: float,
        source_max: float,
        target_min: float,
        target_max: float,
    ) -> float:
        """Convert a value between two numeric ranges."""
        if source_max == source_min:
            return target_min
        ratio = (value - source_min) / (source_max - source_min)
        return target_min + ratio * (target_max - target_min)

    def _property_min_value(self, property_name: str, default: float) -> float:
        """Get a property's numeric minimum."""
        prop = self._property_definition(property_name, use_current_device=False)
        if not prop:
            return default
        for key in ("min", "minimum", "lower", "min_value"):
            raw_value = prop.get(key)
            if isinstance(raw_value, (int, float)):
                return float(raw_value)
            if isinstance(raw_value, str):
                try:
                    return float(raw_value)
                except ValueError:
                    continue
        return default

    def _property_max_value(self, property_name: str, default: float) -> float:
        """Get a property's numeric maximum."""
        prop = self._property_definition(property_name, use_current_device=False)
        if not prop:
            return default
        for key in ("max", "maximum", "upper", "max_value"):
            raw_value = prop.get(key)
            if isinstance(raw_value, (int, float)):
                return float(raw_value)
            if isinstance(raw_value, str):
                try:
                    return float(raw_value)
                except ValueError:
                    continue
        return default

    def _detect_color_temp_mired_mode(self) -> bool:
        """Detect whether color temp values are exposed as mireds.

        Only meaningful when the property is a continuous range; discrete
        preset-index properties are handled separately via ``effect``.
        """
        if not self._color_temp_property or self._color_temp_is_discrete():
            return False
        prop_min = self._property_min_value(self._color_temp_property, 153)
        prop_max = self._property_max_value(self._color_temp_property, 500)
        return max(prop_min, prop_max) <= 1000

    def _color_temp_is_discrete(self) -> bool:
        """Return True if the color-temp-like property is a small preset index.

        Many OEM Ayla-based "tunable white" bulbs (e.g. NOMA iQ) don't expose
        a continuous Kelvin/mired range at all -- their app only shows a
        handful of fixed preset buttons (Candlelight/White/Daylight-style).
        The underlying property in that case is a small integer index (often
        1-5), not a real color temperature. Treating it as continuous causes
        every HA slider position to collapse to the same clamped edge value,
        which looks exactly like "color changes are not applied". We detect
        this by checking whether the property's value span is small enough
        to be an index rather than a real Kelvin/mired range.
        """
        if not self._color_temp_property:
            return False
        prop_min = self._property_min_value(self._color_temp_property, 0)
        prop_max = self._property_max_value(self._color_temp_property, 0)
        span = abs(prop_max - prop_min)
        if 0 < span <= _DISCRETE_COLOR_TEMP_MAX_STEPS:
            return True

        # Some models do not expose min/max. Infer a preset index from small
        # integer-like values commonly used by tunable-white presets.
        if span == 0:
            current_value = self._safe_get_property_value(self._color_temp_property)
            if isinstance(current_value, bool):
                return False
            if isinstance(current_value, (int, float)):
                numeric = float(current_value)
            elif isinstance(current_value, str):
                try:
                    numeric = float(current_value)
                except ValueError:
                    return False
            else:
                return False
            if numeric.is_integer() and 0 <= numeric <= _DISCRETE_COLOR_TEMP_MAX_STEPS + 1:
                return True
        return False

    def _discrete_preset_values(self) -> list[int]:
        """Return the sorted list of raw integer values for discrete presets."""
        prop_min = int(round(self._property_min_value(self._color_temp_property, 1)))
        prop_max = int(round(self._property_max_value(self._color_temp_property, 5)))
        low, high = min(prop_min, prop_max), max(prop_min, prop_max)
        return list(range(low, high + 1))

    def _discrete_preset_names(self) -> list[str]:
        """Return human-friendly names for each discrete preset value."""
        values = self._discrete_preset_values()
        names = _DISCRETE_COLOR_TEMP_PRESET_NAMES.get(len(values))
        if names:
            return list(names)
        return [f"Preset {i + 1}" for i in range(len(values))]

    def _preset_value_to_name(self, value: int) -> str | None:
        values = self._discrete_preset_values()
        names = self._discrete_preset_names()
        try:
            return names[values.index(int(value))]
        except (ValueError, IndexError):
            return None

    def _preset_name_to_value(self, name: str) -> int | None:
        values = self._discrete_preset_values()
        names = self._discrete_preset_names()
        try:
            return values[names.index(name)]
        except (ValueError, IndexError):
            return None

    def _ha_brightness_to_device(self, brightness: int) -> int:
        """Convert HA brightness [0..255] to the device brightness range."""
        if not self._brightness_property:
            return brightness
        prop_min = self._property_min_value(self._brightness_property, 0)
        prop_max = self._property_max_value(self._brightness_property, 100)
        mapped = self._scale_value(
            self._clamp(float(brightness), 0, 255),
            source_min=0,
            source_max=255,
            target_min=prop_min,
            target_max=prop_max,
        )
        return int(round(self._clamp(mapped, min(prop_min, prop_max), max(prop_min, prop_max))))

    def _device_brightness_to_ha(self, brightness: float) -> int:
        """Convert device brightness range back to HA [0..255]."""
        if not self._brightness_property:
            return int(brightness)
        prop_min = self._property_min_value(self._brightness_property, 0)
        prop_max = self._property_max_value(self._brightness_property, 100)
        mapped = self._scale_value(
            float(brightness),
            source_min=prop_min,
            source_max=prop_max,
            target_min=0,
            target_max=255,
        )
        return int(round(self._clamp(mapped, 0, 255)))

    def _color_temp_kelvin_to_device(self, kelvin: int) -> int:
        """Convert HA color temp (kelvin) to device property value."""
        if not self._color_temp_property:
            return kelvin
        if self._color_temp_uses_mired:
            mired = color_util.color_temperature_kelvin_to_mired(kelvin)
            prop_min = self._property_min_value(self._color_temp_property, 153)
            prop_max = self._property_max_value(self._color_temp_property, 500)
            return int(
                round(self._clamp(float(mired), min(prop_min, prop_max), max(prop_min, prop_max)))
            )
        prop_min = self._property_min_value(self._color_temp_property, 2000)
        prop_max = self._property_max_value(self._color_temp_property, 6500)
        return int(
            round(self._clamp(float(kelvin), min(prop_min, prop_max), max(prop_min, prop_max)))
        )

    def _color_temp_device_to_kelvin(self, device_value: float) -> int:
        """Convert device color temp value to HA kelvin."""
        if self._color_temp_uses_mired:
            return color_util.color_temperature_mired_to_kelvin(int(device_value))
        return int(device_value)

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        value = self._safe_get_property_value(self._power_property)
        return self._coerce_is_on(value) if value is not None else None

    @property
    def brightness(self) -> int | None:
        """Return current brightness in HA's 0..255 scale."""
        if not self._brightness_property:
            return None
        value = self._safe_get_property_value(self._brightness_property)
        if value is None:
            return None
        return self._device_brightness_to_ha(float(value))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return current color as (hue,sat)."""
        if not self._hue_property or not self._saturation_property:
            return None
        hue = self._safe_get_property_value(self._hue_property)
        saturation = self._safe_get_property_value(self._saturation_property)
        if hue is None or saturation is None:
            return None
        hue_min = self._property_min_value(self._hue_property, 0)
        hue_max = self._property_max_value(self._hue_property, 360)
        sat_min = self._property_min_value(self._saturation_property, 0)
        sat_max = self._property_max_value(self._saturation_property, 100)
        ha_hue = self._scale_value(
            float(hue),
            source_min=hue_min,
            source_max=hue_max,
            target_min=0,
            target_max=360,
        )
        ha_sat = self._scale_value(
            float(saturation),
            source_min=sat_min,
            source_max=sat_max,
            target_min=0,
            target_max=100,
        )
        return (
            self._clamp(ha_hue, 0, 360),
            self._clamp(ha_sat, 0, 100),
        )

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return color temperature in kelvin."""
        if not self._color_temp_property or self._color_temp_is_discrete():
            return None
        value = self._safe_get_property_value(self._color_temp_property)
        if value is None:
            return None
        kelvin = self._color_temp_device_to_kelvin(float(value))
        return int(
            self._clamp(
                float(kelvin),
                float(self.min_color_temp_kelvin),
                float(self.max_color_temp_kelvin),
            )
        )

    @property
    def effect(self) -> str | None:
        """Return the current discrete white-temperature preset, if applicable."""
        if not self._color_temp_property or not self._color_temp_is_discrete():
            return None
        value = self._safe_get_property_value(self._color_temp_property)
        if value is None:
            return None
        try:
            return self._preset_value_to_name(int(value))
        except (TypeError, ValueError):
            return None

    def _property_is_writable(self, property_name: str) -> bool:
        """Check whether a property is known and writable.

        Ayla device schemas vary between models: some expose a distinct
        ``SET_*``-prefixed input property, others reuse the same property
        name for both read and write, and the ``read_only`` flag on the
        read/output property is not a reliable signal for whether the
        underlying feature is actually controllable. Pre-emptively blocking
        writes based on that flag caused color/color-temperature updates to
        be silently dropped on bulbs that expose those flags this way (while
        on/off and brightness happened to be marked writable and worked
        fine). We now only refuse to write when the property is entirely
        unknown; genuine write rejections are surfaced via the API error
        instead of guessed at client-side.
        """
        prop = self._property_definition(property_name, use_current_device=False)
        return prop is not None

    def _update_cached_property_value(self, property_name: str, value: int | float) -> None:
        """Update local and coordinator-cached property values optimistically."""
        for device in (self._device, self._get_current_device()):
            if not device:
                continue
            properties = getattr(device, "properties_full", None)
            if not hasattr(properties, "get"):
                continue
            local_property = properties.get(property_name)
            if isinstance(local_property, dict):
                local_property["value"] = value

    async def _safe_async_set_property_value(
        self, property_name: str, value: int | float
    ) -> bool:
        """Set property while validating metadata and avoiding invalid writes."""
        prop = self._property_definition(property_name, use_current_device=False)
        if not prop:
            _LOGGER.debug("Skipping unsupported property write: %s", property_name)
            return False
        api_property_name = self._resolve_write_property_api_name(property_name, prop)
        if not api_property_name:
            _LOGGER.warning(
                "Skipping property write for %s: no API property name could be resolved "
                "(known keys: %s)",
                property_name,
                sorted(getattr(self._device, "properties_full", {}) or {}),
            )
            return False

        endpoint = self._device.set_property_endpoint(api_property_name)
        payload = {"datapoint": {"value": value}}
        async with await self.coordinator.api.async_request(
            "post",
            endpoint,
            json=payload,
        ) as response:
            response_data = await response.json()

        # Best-effort local cache update to keep optimistic state in sync.
        properties = getattr(self._device, "properties_full", None)
        if hasattr(properties, "get"):
            local_property = properties.get(property_name)
            if isinstance(local_property, dict):
                local_property.update(response_data)
        self._update_cached_property_value(property_name, value)
        return True

    async def _queue_updates(self, updates: dict[str, int | float]) -> None:
        """Coalesce rapid updates into a debounced flush."""
        async with self._pending_lock:
            self._pending_updates.update(updates)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._async_flush_pending_updates())
            task = self._flush_task
        await task

    async def _async_flush_pending_updates(self) -> None:
        """Apply queued updates with throttled API writes."""
        try:
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            while True:
                async with self._pending_lock:
                    if not self._pending_updates:
                        break
                    updates = self._pending_updates.copy()
                    self._pending_updates.clear()

                has_state_change = False
                for index, (property_name, value) in enumerate(updates.items()):
                    if not self._property_is_writable(property_name):
                        continue
                    if self._safe_get_property_value(property_name) == value:
                        continue
                    try:
                        has_state_change = (
                            await self._safe_async_set_property_value(property_name, value)
                            or has_state_change
                        )
                    except Exception as ex:  # noqa: BLE001
                        _LOGGER.warning(
                            "Failed setting %s for device %s: %s",
                            property_name,
                            self._device.serial_number,
                            ex,
                        )
                    if index < len(updates) - 1:
                        await asyncio.sleep(_INTER_PROPERTY_DELAY_SECONDS)

                if has_state_change:
                    self.async_write_ha_state()

                # If more updates were queued while writing, debounce briefly and apply again.
                async with self._pending_lock:
                    has_more_updates = bool(self._pending_updates)
                if not has_more_updates:
                    break
                await asyncio.sleep(_DEBOUNCE_SECONDS)

            await self.coordinator.async_request_refresh()
        finally:
            async with self._pending_lock:
                if asyncio.current_task() is self._flush_task:
                    self._flush_task = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn device on."""
        updates: dict[str, int | float] = {self._power_property: 1}

        if self._brightness_property and ATTR_BRIGHTNESS in kwargs:
            updates[self._brightness_property] = self._ha_brightness_to_device(
                int(kwargs[ATTR_BRIGHTNESS])
            )
            if self._attr_color_mode == ColorMode.ONOFF:
                self._attr_color_mode = ColorMode.BRIGHTNESS

        if self._hue_property and self._saturation_property and ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            hue_min = self._property_min_value(self._hue_property, 0)
            hue_max = self._property_max_value(self._hue_property, 360)
            sat_min = self._property_min_value(self._saturation_property, 0)
            sat_max = self._property_max_value(self._saturation_property, 100)
            updates[self._hue_property] = int(
                round(
                    self._clamp(
                        self._scale_value(
                            float(hue),
                            source_min=0,
                            source_max=360,
                            target_min=hue_min,
                            target_max=hue_max,
                        ),
                        min(hue_min, hue_max),
                        max(hue_min, hue_max),
                    )
                )
            )
            updates[self._saturation_property] = int(
                round(
                    self._clamp(
                        self._scale_value(
                            float(saturation),
                            source_min=0,
                            source_max=100,
                            target_min=sat_min,
                            target_max=sat_max,
                        ),
                        min(sat_min, sat_max),
                        max(sat_min, sat_max),
                    )
                )
            )
            self._attr_color_mode = ColorMode.HS
        elif ATTR_HS_COLOR in kwargs:
            _LOGGER.warning(
                "Ignoring HS color update for %s: hue/saturation properties not available",
                self._device.serial_number,
            )

        if (
            self._color_temp_property
            and not self._color_temp_is_discrete()
            and (ATTR_COLOR_TEMP_KELVIN in kwargs or ATTR_COLOR_TEMP in kwargs)
        ):
            kelvin = None
            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            else:
                # Legacy HA color temp attribute in mireds.
                kelvin = color_util.color_temperature_mired_to_kelvin(int(kwargs[ATTR_COLOR_TEMP]))
            updates[self._color_temp_property] = self._color_temp_kelvin_to_device(
                kelvin
            )
            if ColorMode.HS not in self.supported_color_modes or ATTR_HS_COLOR not in kwargs:
                self._attr_color_mode = ColorMode.COLOR_TEMP

        if (
            self._color_temp_property
            and self._color_temp_is_discrete()
            and (
                ATTR_EFFECT in kwargs
                or ATTR_COLOR_TEMP_KELVIN in kwargs
                or ATTR_COLOR_TEMP in kwargs
            )
        ):
            if ATTR_EFFECT in kwargs:
                preset_value = self._preset_name_to_value(kwargs[ATTR_EFFECT])
                if preset_value is None:
                    _LOGGER.warning(
                        "Unknown white-temperature preset %r for %s (available: %s)",
                        kwargs[ATTR_EFFECT],
                        self._device.serial_number,
                        self._discrete_preset_names(),
                    )
                else:
                    updates[self._color_temp_property] = preset_value
            else:
                if ATTR_COLOR_TEMP_KELVIN in kwargs:
                    kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
                else:
                    kelvin = color_util.color_temperature_mired_to_kelvin(
                        int(kwargs[ATTR_COLOR_TEMP])
                    )
                values = self._discrete_preset_values()
                if values:
                    target = self._color_temp_kelvin_to_device(kelvin)
                    nearest = min(values, key=lambda value: abs(value - target))
                    updates[self._color_temp_property] = nearest

        await self._queue_updates(updates)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn device off."""
        await self._queue_updates({self._power_property: 0})

    async def async_update(self) -> None:
        """Update the light state."""
        await self.coordinator.async_request_refresh()
