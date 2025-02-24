"""Support for Vulcan sensors."""

import datetime
import logging
from asyncio import timeout
from datetime import timedelta

from aiohttp import ClientConnectorError
from homeassistant.components import persistent_notification
from homeassistant.components.sensor import ENTITY_ID_FORMAT
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from vulcan import UnauthorizedCertificateException

from . import DOMAIN, VulcanEntity
from .const import (
    CONF_ATTENDANCE_NOTIFY,
    CONF_GRADE_NOTIFY,
    CONF_LESSON_ENTITIES_NUMBER,
    CONF_MESSAGE_NOTIFY,
    DEFAULT_LESSON_ENTITIES_NUMBER,
    DEFAULT_SCAN_INTERVAL,
)
from .fetch_data import (
    get_latest_attendance,
    get_latest_grade,
    get_latest_message,
    get_lessons,
    get_lucky_number,
    get_next_exam,
    get_next_homework,
    get_student_info,
)

SCAN_INTERVAL = timedelta(minutes=DEFAULT_SCAN_INTERVAL)
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Vulcan sensor entry."""
    global SCAN_INTERVAL
    SCAN_INTERVAL = (
        timedelta(minutes=config_entry.options.get(CONF_SCAN_INTERVAL))
        if config_entry.options.get(CONF_SCAN_INTERVAL) is not None
        else SCAN_INTERVAL
    )
    client = hass.data[DOMAIN][config_entry.entry_id]

    async def async_update_data():
        try:
            async with timeout(30):
                return {
                    "lessons": await get_lessons(
                        client,
                        date_from=datetime.date.today(),
                        entities_number=config_entry.options.get(
                            CONF_LESSON_ENTITIES_NUMBER, DEFAULT_LESSON_ENTITIES_NUMBER
                        ),
                    ),
                    "lessons_t": await get_lessons(
                        client,
                        date_from=datetime.date.today() + timedelta(days=1),
                        entities_number=config_entry.options.get(
                            CONF_LESSON_ENTITIES_NUMBER, DEFAULT_LESSON_ENTITIES_NUMBER
                        ),
                    ),
                }
        except UnauthorizedCertificateException:
            _LOGGER.error(
                "The certificate is not authorized, please authorize integration again."
            )
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "reauth"},
                )
            )
        except ClientConnectorError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            raise UpdateFailed(err) from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="vulcan_timetable",
        update_method=async_update_data,
        update_interval=SCAN_INTERVAL,
    )
    await coordinator.async_refresh()

    try:
        data = {
            "student_info": await get_student_info(
                client, config_entry.data.get("student_id")
            ),
            "students_number": hass.data[DOMAIN]["students_number"],
            "grade": await get_latest_grade(client),
            "message": await get_latest_message(client),
            "lucky_number": await get_lucky_number(client),
            "attendance": await get_latest_attendance(client),
            "homework": await get_next_homework(client),
            "exam": await get_next_exam(client),
            "notify": {
                CONF_MESSAGE_NOTIFY: config_entry.options.get(CONF_MESSAGE_NOTIFY),
                CONF_GRADE_NOTIFY: config_entry.options.get(CONF_GRADE_NOTIFY),
                CONF_ATTENDANCE_NOTIFY: config_entry.options.get(
                    CONF_ATTENDANCE_NOTIFY
                ),
            },
        }
    except ClientConnectorError as err:
        if "connection_error" not in hass.data[DOMAIN]:
            _LOGGER.error(
                "Connection error - please check your internet connection: %s", err
            )
            hass.data[DOMAIN]["connection_error"] = True
        raise PlatformNotReady from err

    entities = [
        LatestGrade(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"latest_grade_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
        LuckyNumber(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"lucky_number_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
        LatestAttendance(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"latest_attendance_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
        LatestMessage(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"latest_message_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
        NextHomework(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"next_homework_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
        NextExam(
            client,
            data,
            generate_entity_id(
                ENTITY_ID_FORMAT,
                f"next_exam_{data['student_info']['full_name']}",
                hass=hass,
            ),
        ),
    ]
    for i in range(
        config_entry.options.get(
            CONF_LESSON_ENTITIES_NUMBER, DEFAULT_LESSON_ENTITIES_NUMBER
        )
    ):
        entities.append(
            VulcanLessonEntity(
                coordinator,
                data,
                i,
                generate_entity_id(
                    ENTITY_ID_FORMAT,
                    f"lesson_{i}_{data['student_info']['full_name']}",
                    hass=hass,
                ),
            )
        )
        entities.append(
            VulcanLessonEntity(
                coordinator,
                data,
                i,
                generate_entity_id(
                    ENTITY_ID_FORMAT,
                    f"lesson_{i}_tomorrow_{data['student_info']['full_name']}",
                    hass=hass,
                ),
                True,
            )
        )

    async_add_entities(entities)


class VulcanLessonEntity(CoordinatorEntity, VulcanEntity):
    """Represents a lesson entity for the Vulcan integration."""

    def __init__(self, coordinator, data, number, entity_id, is_tomorrow=False):
        """Initialize the VulcanLessonEntity class.

        Args:
            coordinator (Coordinator): The coordinator object.
            data (dict): The data dictionary.
            number (int): The lesson number.
            entity_id (str): The entity ID.
            is_tomorrow (bool, optional): Flag indicating if the lesson is for tomorrow. Defaults to False.

        """

        super().__init__(coordinator)
        self.entity_id = entity_id
        self.is_tomorrow = is_tomorrow
        self.student_info = data["student_info"]
        self.student_name = self.student_info["full_name"]
        self.student_id = str(self.student_info["id"])

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "

        self.number = str(number)
        if self.is_tomorrow is True:
            self.tomorrow = "_t"
            name_tomorrow = " (Tomorrow)"
            self.tomorrow_device_id = "tomorrow_"
            self.device_name_tomorrow = "Tomorrow "
            self.num_tomorrow = timedelta(days=1)
        else:
            self.tomorrow = ""
            name_tomorrow = " "
            self.tomorrow_device_id = ""
            self.device_name_tomorrow = ""
            self.num_tomorrow = timedelta(days=0)

        if number >= 10:
            space = chr(160)
        else:
            space = " "

        self._name = f"Lesson{space}{self.number}{name_tomorrow}{name}"
        self._unique_id = f"lesson_{self.tomorrow}{self.number}_{self.student_id}"
        self._icon = "mdi:timetable"

    @property
    def state(self):
        """Return the state of the lesson."""
        return self.coordinator.data[f"lessons{self.tomorrow}"][
            f"lesson_{self.number}"
        ]["lesson"]

    @property
    def available(self) -> bool:
        """Check if the lesson is available."""
        if not self.coordinator.last_update_success:
            if not self.is_tomorrow:
                if (
                    self.coordinator.data[f"lessons{self.tomorrow}"][
                        f"lesson_{self.number}"
                    ]["date"]
                    != datetime.date.today()
                ):
                    return False
            elif self.coordinator.data[f"lessons{self.tomorrow}"][
                f"lesson_{self.number}"
            ]["date"] != datetime.date.today() + timedelta(days=1):
                return False
        return True

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the lesson."""
        lesson_info = self.coordinator.data[f"lessons{self.tomorrow}"][
            f"lesson_{self.number}"
        ]
        atr = {
            "room": lesson_info["room"],
            "teacher": lesson_info["teacher"],
            "time": lesson_info["from_to"],
            # "changes": lesson_info["changes"],
            "reason": lesson_info["reason"],
        }
        return atr

    @property
    def device_info(self):
        """Return device information for the Timetable."""
        return {
            "identifiers": {
                (DOMAIN, f"{self.tomorrow_device_id}timetable_{self.student_id}")
            },
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}{self.device_name_tomorrow}Timetable",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }


class LatestAttendance(VulcanEntity):
    """Represents the latest attendance for a student."""

    def __init__(self, client, data, entity_id):
        """Initialize the Vulcan sensor."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.student_id = str(self.student_info["id"])
        self.latest_attendance = data["attendance"]
        self.notify = data["notify"][CONF_ATTENDANCE_NOTIFY]
        self.old_att = self.latest_attendance["datetime"]
        self._state = self.latest_attendance["content"]

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "
        self._name = f"Latest Attendance{name}"
        self._unique_id = f"attendance_latest_{self.student_id}"
        self._icon = "mdi:account-check-outline"

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the attendance."""
        att_info = self.latest_attendance
        atr = {
            "Lesson": att_info["lesson_name"],
            "Lesson number": att_info["lesson_number"],
            "Lesson date": att_info["lesson_date"],
            "Lesson time": att_info["lesson_time"],
        }

        return atr

    @property
    def device_info(self):
        """Return device information for the Attendance."""
        return {
            "identifiers": {(DOMAIN, f"attendance{self.student_id}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Attendance",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the sensor state."""
        try:
            self.latest_attendance = await get_latest_attendance(self.client)
        except Exception:
            self.latest_attendance = await get_latest_attendance(self.client)
        latest_attendance = self.latest_attendance
        if (
            self.latest_attendance["content"] != "-"
            and self.old_att < self.latest_attendance["datetime"]
        ):
            if self.notify is True and self.latest_attendance["content"] != "obecność":
                persistent_notification.async_create(
                    self.hass,
                    f"{self.latest_attendance['lesson_time']}, {self.latest_attendance['lesson_date']}\n{self.latest_attendance['content']}",
                    f"{self.device_student_name}Vulcan: Nowy wpis frekwencji na lekcji {self.latest_attendance['lesson_name']}",
                )
            self.old_att = self.latest_attendance["datetime"]
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device(
                identifiers={(DOMAIN, f"attendance{self.student_id}")}
            )
            if device_entry:
                event_data = {
                    "device_id": device_entry.id,
                    "type": "new_attendance",
                }
                self.hass.bus.async_fire("vulcan_event", event_data)
        self._state = latest_attendance["content"]


class LatestMessage(VulcanEntity):
    """Represents the latest message entity."""

    def __init__(self, client, data, entity_id):
        """Initialize the sensor."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.latest_message = data["message"]
        self.notify = data["notify"][CONF_MESSAGE_NOTIFY]
        self.old_msg = self.latest_message["id"]
        self._state = self.latest_message["title"][0:250]

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "
        self._name = f"Latest Message{name}"
        self._unique_id = f"message_latest_{self.student_info['id']}"
        self._icon = "mdi:message-arrow-left-outline"

    @property
    def extra_state_attributes(self):
        """Return extra state attributes for the sensor."""
        msg_info = self.latest_message
        atr = {
            "Sender": msg_info["sender"],
            "Date": msg_info["date"],
            "Content": msg_info["content"],
        }

        return atr

    @property
    def device_info(self):
        """Return device information for the sensor."""
        return {
            "identifiers": {(DOMAIN, f"message{self.student_info['id']}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Messages",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the sensor state with the latest message."""
        try:
            self.latest_message = await get_latest_message(self.client)
        except Exception:
            self.latest_message = await get_latest_message(self.client)
        if self.old_msg != self.latest_message["id"] and self.latest_message["id"] != 0:
            if self.notify is True:
                persistent_notification.async_create(
                    self.hass,
                    f"{self.latest_message['sender']}, {self.latest_message['date']}\n{self.latest_message['content']}",
                    f"Vulcan: {self.latest_message['title']}",
                )
            self.old_msg = self.latest_message["id"]
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device(
                identifiers={(DOMAIN, f"message{self.student_info['id']}")},
            )
            if device_entry:
                event_data = {
                    "device_id": device_entry.id,
                    "type": "new_message",
                }
                self.hass.bus.async_fire("vulcan_event", event_data)
        self._state = self.latest_message["title"][0:250]


class LatestGrade(VulcanEntity):
    """Represents the latest grade entity."""

    def __init__(self, client, data, entity_id):
        """Initialize the sensor."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.latest_grade = data["grade"]
        self._state = self.latest_grade["content"]
        self.student_id = str(self.student_info["id"])
        self.notify = data["notify"][CONF_GRADE_NOTIFY]
        self.old_state = f"{self.latest_grade['content']}_{self.latest_grade['subject']}_{self.latest_grade['date']}_{self.latest_grade['description']}"

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "

        self._name = f"Latest grade{name}"
        self._unique_id = f"grade_latest_{self.student_id}"
        self._icon = "mdi:school-outline"

    @property
    def extra_state_attributes(self):
        """Return extra state attributes for the sensor."""
        grade_info = self.latest_grade
        atr = {
            "subject": grade_info["subject"],
            "weight": grade_info["weight"],
            "teacher": grade_info["teacher"],
            "date": grade_info["date"],
            "description": grade_info["description"],
        }
        return atr

    @property
    def device_info(self):
        """Return device information for the sensor."""
        return {
            "identifiers": {(DOMAIN, f"grade{self.student_id}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Grades",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the sensor state with the latest grade information."""
        try:
            self.latest_grade = await get_latest_grade(self.client)
        except Exception:
            self.latest_grade = await get_latest_grade(self.client)
        if (
            self.latest_grade["content"] != "-"
            and self.old_state
            != f"{self.latest_grade['content']}_{self.latest_grade['subject']}_{self.latest_grade['date']}_{self.latest_grade['description']}"
        ):
            if self.notify is True:
                persistent_notification.async_create(
                    self.hass,
                    f"Nowa ocena {self.latest_grade['content']} z {self.latest_grade['subject']} została wystawiona {self.latest_grade['date']} przez {self.latest_grade['teacher']}.",
                    f"{self.device_student_name}Vulcan: Nowa ocena z {self.latest_grade['subject']}: {self.latest_grade['content']}",
                )
            self.old_state = f"{self.latest_grade['content']}_{self.latest_grade['subject']}_{self.latest_grade['date']}_{self.latest_grade['description']}"
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device(
                identifiers={(DOMAIN, f"message{self.student_info['id']}")},
            )
            if device_entry:
                event_data = {
                    "device_id": device_entry.id,
                    "type": "new_grade",
                }
                self.hass.bus.async_fire("vulcan_event", event_data)
        self._state = self.latest_grade["content"]


class NextHomework(VulcanEntity):
    """Represents the next homework for a student."""

    def __init__(self, client, data, entity_id):
        """Initialize the VulcanEntity class."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.student_name = self.student_info["full_name"]
        self.student_id = str(self.student_info["id"])
        self.next_homework = data["homework"]
        self._state = self.next_homework["description"][0:250]

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "

        self._name = f"Next Homework{name}"
        self._unique_id = f"homework_next_{self.student_id}"
        self._icon = "mdi:pen"

    @property
    def extra_state_attributes(self):
        """Return extra state attributes for the sensor."""
        atr = {
            "subject": self.next_homework["subject"],
            "teacher": self.next_homework["teacher"],
            "date": self.next_homework["date"],
        }
        return atr

    @property
    def device_info(self):
        """Return device information for the sensor."""
        return {
            "identifiers": {(DOMAIN, f"homework{self.student_id}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Homeworks",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the state of the sensor with the next homework information."""
        try:
            self.next_homework = await get_next_homework(self.client)
        except Exception:
            self.next_homework = await get_next_homework(self.client)
        self._state = self.next_homework["description"][0:250]


class NextExam(VulcanEntity):
    """Represents the next exam for a student."""

    def __init__(self, client, data, entity_id):
        """Initialize the NextExam class."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.student_name = self.student_info["full_name"]
        self.student_id = str(self.student_info["id"])
        self.next_exam = data["exam"]
        self._state = self.next_exam["description"][0:250]

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "

        self._name = f"Next Exam{name}"
        self._unique_id = f"exam_next_{self.student_id}"
        self._icon = "mdi:format-list-checks"

    @property
    def extra_state_attributes(self):
        """Return extra state attributes for the sensor."""
        atr = {
            "subject": self.next_exam["subject"],
            "type": self.next_exam["type"],
            "teacher": self.next_exam["teacher"],
            "date": self.next_exam["date"],
        }
        return atr

    @property
    def device_info(self):
        """Return device information for the sensor."""
        return {
            "identifiers": {(DOMAIN, f"exam{self.student_id}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Exam",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the state of the sensor with the next exam information."""
        try:
            self.next_exam = await get_next_exam(self.client)
        except Exception:
            self.next_exam = await get_next_exam(self.client)
        self._state = self.next_exam["description"][0:250]


class LuckyNumber(VulcanEntity):
    """Represents the lucky number for a student."""

    def __init__(self, client, data, entity_id):
        """Initialize the LuckyNumber class."""
        self.entity_id = entity_id
        self.client = client
        self.student_info = data["student_info"]
        self.student_name = self.student_info["full_name"]
        self.student_id = str(self.student_info["id"])
        self.lucky_number = data["lucky_number"]
        self._state = self.lucky_number["number"]

        if data["students_number"] == 1:
            name = ""
            self.device_student_name = ""
        else:
            name = f" - {self.student_info['full_name']}"
            self.device_student_name = f"{self.student_info['full_name']}: "

        self._name = f"Lucky Number{name}"
        self._unique_id = f"lucky_number_{self.student_id}"
        self._icon = "mdi:ticket-confirmation-outline"

    @property
    def extra_state_attributes(self):
        """Return extra state attributes for the sensor."""
        atr = {
            "date": self.lucky_number["date"],
        }
        return atr

    @property
    def device_info(self):
        """Return device information for the sensor."""
        return {
            "identifiers": {(DOMAIN, f"lucky_number{self.student_id}")},
            "manufacturer": "Uonet +",
            "model": f"{self.student_info['full_name']} - {self.student_info['class']} {self.student_info['school']}",
            "name": f"{self.device_student_name}Lucky Number",
            "entry_type": DeviceEntryType.SERVICE,
            "configuration_url": f"https://uonetplus.vulcan.net.pl/{self.student_info['symbol']}",
        }

    async def async_update(self):
        """Update the state of the sensor."""
        try:
            self.lucky_number = await get_lucky_number(self.client)
        except Exception:
            self.lucky_number = await get_lucky_number(self.client)
        self._state = self.lucky_number["number"]
