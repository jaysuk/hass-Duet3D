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
from .jobinfo import pick_thumbnail
from .webcam import read_frame
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import logging
import io
from PIL import Image

_LOGGER = logging.getLogger(__name__)
from .const import (
    CONF_STANDALONE,
    DOMAIN,
    SENSOR_TYPES,
)

# A thumbnail is a few kB. Anything beyond this much base64 text is not one.
_MAX_THUMBNAIL_CHARS = 1024 * 1024
_MAX_THUMBNAIL_CHUNKS = 1000


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


def convert_qoi_to_jpeg(qoi_data: bytes) -> bytes:
    """Re-encode a QOI image (which browsers and most dashboards cannot show) as JPEG."""
    image = Image.open(io.BytesIO(qoi_data)).convert("RGB")
    with io.BytesIO() as output:
        image.save(output, format="JPEG")
        return output.getvalue()


class DuetThumbnailCamera(CoordinatorEntity[DuetDataUpdateCoordinator], Camera):
    """A camera to show the Duet3D thumbnail image."""

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
        # The last image, as converted, and the (file name, thumbnail offset) it is of.
        self._cached_key: tuple | None = None
        self._cached_image: bytes | None = None

    @property
    def device_info(self):
        """Device info."""
        return self.coordinator.device_info

    def _thumbnail(self) -> dict | None:
        """The thumbnail to show: one with inline data in SBC mode, else one to fetch."""
        return pick_thumbnail(
            self.coordinator.get_sensor_state(
                SENSOR_TYPES[self.camera_name]["json_path"], self.camera_name
            ),
            inline=not self.coordinator.config_entry.data[CONF_STANDALONE],
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._thumbnail() is not None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        thumbnail = self._thumbnail()
        if thumbnail is None:
            return None
        file_name = self.coordinator.get_sensor_state("status.job.file.fileName") or ""
        key = (file_name, thumbnail.get("offset"))
        if key == self._cached_key:
            return self._cached_image

        try:
            if self.coordinator.config_entry.data[CONF_STANDALONE]:
                text = await self._fetch(file_name, thumbnail["offset"])
            else:
                text = thumbnail["data"]
            data = None if text is None else base64.b64decode("".join(text.split()))
            if data is not None and data.startswith(b"qoif"):
                data = await self.hass.async_add_executor_job(convert_qoi_to_jpeg, data)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not read the thumbnail of %s: %s", file_name, exc)
            return None
        if data is None:
            return None
        self._cached_key, self._cached_image = key, data
        return data

    async def _fetch(self, file_name: str, offset: int) -> str | None:
        """The thumbnail as base64 text, read from the board in the chunks it serves.

        The chunks are joined before decoding: a chunk need not end on a multiple of
        four characters.
        """
        chunks: list[str] = []
        total = 0
        for _ in range(_MAX_THUMBNAIL_CHUNKS):
            response = await self.coordinator.fetch_json(
                f"{self.coordinator.base_url}/rr_thumbnail",
                {"name": file_name, "offset": offset},
            )
            if not isinstance(response, dict) or response.get("err"):
                return None
            data = response.get("data")
            if not isinstance(data, str):
                return None
            chunks.append(data)
            total += len(data)
            if total > _MAX_THUMBNAIL_CHARS:
                return None
            offset = response.get("next") or 0
            if not offset:
                return "".join(chunks)
        return None


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
