from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntry
import asyncio
import base64
import aiohttp
from . import DuetDataUpdateCoordinator
from .entity import add_dynamic
from .webcam import read_frame
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import logging
import io
from PIL import Image

_LOGGER = logging.getLogger(__name__)
from .const import (
    DOMAIN,
    SENSOR_TYPES,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coordinator: DuetDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]
    device_id = config_entry.entry_id
    assert device_id is not None
    async_add_entities([DuetThumbnailCamera(coordinator, "Thumbnail", device_id)])

    # Only when an address is known, now or (DWC settings are read once, at setup) never.
    def webcam():
        if coordinator.webcam_url:
            yield "webcam", lambda: DuetWebcamCamera(coordinator, device_id)

    add_dynamic(coordinator, config_entry, async_add_entities, webcam)


class DuetThumbnailCamera(CoordinatorEntity[DuetDataUpdateCoordinator], Camera):
    """A camera to show the Duet3D thumbnail image."""

    _attr_is_streaming = True
    _attr_motion_detection_enabled = False
    _attr_supported_features = CameraEntityFeature.ON_OFF

    def __init__(
        self,
        coordinator: DuetDataUpdateCoordinator,
        camera_name: str,
        device_id: str,
    ) -> None:
        """Initialize a new Duet thumbnail camera."""
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._device_id = device_id
        self._attr_name = f"{self.device_info['name']} {camera_name}"
        self._attr_unique_id = device_id
        self.camera_name = camera_name
        self.last_thumbnail_data = ""
        self.last_image: bytes

    @property
    def device_info(self):
        """Device info."""
        return self.coordinator.device_info

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        job_thumbnail = self.coordinator.get_sensor_state(
            SENSOR_TYPES[self.camera_name]["json_path"], self.camera_name
        )
        # Only DSF (SBC mode) embeds the image; rr_model lists thumbnails without data.
        return (
            isinstance(job_thumbnail, list)
            and len(job_thumbnail) > 0
            and isinstance(job_thumbnail[0], dict)
            and "data" in job_thumbnail[0]
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        thumbnail_info_json_path = SENSOR_TYPES[self.camera_name]["json_path"]
        thumbnail_info = self.coordinator.get_sensor_state(
            thumbnail_info_json_path, self.camera_name
        )
        if self.available:
            thumbnail_data = base64.b64decode(thumbnail_info[0]["data"])
            if b"qoi" in thumbnail_data:
                thumbnail_data = self.convert_qoi_to_jpeg(thumbnail_data)

            if self.last_thumbnail_data == thumbnail_data:
                return self.last_image

            self.last_image = thumbnail_data
            return self.last_image

    def convert_qoi_to_jpeg(self, qoi_data):
        # Load QOI image from bytes
        qoi_image = Image.open(io.BytesIO(qoi_data)).convert("RGB")
        # Convert QOI image to JPEG format
        with io.BytesIO() as output:
            qoi_image.save(output, format="JPEG")
            return output.getvalue()


class DuetWebcamCamera(CoordinatorEntity[DuetDataUpdateCoordinator], Camera):
    """The printer's webcam, from the address in the options or in Duet Web Control."""

    _attr_motion_detection_enabled = False

    def __init__(self, coordinator: DuetDataUpdateCoordinator, device_id: str) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_name = f"{self.device_info['name']} Webcam"
        self._attr_unique_id = f"webcam-{device_id}"

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def supported_features(self) -> CameraEntityFeature:
        return CameraEntityFeature.STREAM if self.coordinator.webcam_live_url else CameraEntityFeature(0)

    async def stream_source(self) -> str | None:
        return self.coordinator.webcam_live_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """One frame: the snapshot address read whole, or the first frame of a stream."""
        url = self.coordinator.webcam_url
        if not url:
            return None
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(10):
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await read_frame(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("Could not read a frame from %s: %s", url, exc)
            return None
