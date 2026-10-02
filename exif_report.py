"""Читает метаданные файла через ExifTool и собирает из них короткий отчёт."""

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from html import escape
from pathlib import Path

EXIFTOOL_TIMEOUT = 30  # секунд

# Эти группы описывают сам файл (тип, размер) или вычислены самим ExifTool,
# поэтому в число тегов метаданных их не считаем
SERVICE_GROUPS = {"ExifTool", "File", "Composite"}

DIRECTIONS = ["север", "северо-восток", "восток", "юго-восток",
              "юг", "юго-запад", "запад", "северо-запад"]

# «2026:09:15 18:42:07.123+03:00» — так ExifTool отдаёт даты
DATE_RE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?")


class ExifToolError(Exception):
    pass


@dataclass
class Report:
    text: str
    location: tuple[float, float] | None = None


def find_exiftool() -> str | None:
    """Ищет ExifTool: переменная EXIFTOOL, exiftool.exe рядом с ботом, затем PATH."""
    if path := os.getenv("EXIFTOOL"):
        return shutil.which(path)
    local = Path(__file__).with_name("exiftool.exe")
    if local.exists():
        return str(local)
    return shutil.which("exiftool")


async def read_metadata(data: bytes, exiftool: str) -> dict:
    """Отдаёт файл ExifTool через stdin, так что на диск он не попадает."""
    proc = await asyncio.create_subprocess_exec(
        # -n: числа вместо текста («0.0083» вместо «1/120»), -G: группа перед именем тега
        exiftool, "-json", "-n", "-G", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), EXIFTOOL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ExifToolError("слишком долго разбирал файл") from None
    try:
        tags = json.loads(out)[0]
    except (ValueError, IndexError):
        raise ExifToolError(err.decode(errors="replace").strip() or "пустой ответ") from None
    if error := tags.get("ExifTool:Error"):
        raise ExifToolError(error)
    return tags


def build_report(tags: dict, size: int, compressed: bool = False) -> Report:
    location = _coordinates(tags)
    personal = [s for s in (_when(tags), _device(tags), _author(tags)) if s]
    sections = [_where(tags, *location)] if location else []
    sections += personal
    if shot := _shot(tags):
        sections.append(shot)
    sections.append(_file(tags, size))

    if location:
        verdict = "🔴 <b>В файле есть координаты места съёмки</b>"
    elif personal:
        verdict = "🟡 <b>Координат нет, но кое-что узнать можно</b>"
    else:
        verdict = "🟢 <b>Ни координат, ни даты, ни данных об устройстве в файле нет</b>"
    count = sum(1 for key in tags if ":" in key and key.split(":")[0] not in SERVICE_GROUPS)

    header = f"{verdict}\nТегов метаданных в файле: {count}"
    if compressed:
        header = ("ℹ️ Это сжатое фото: Telegram пересобрал его и вырезал метаданные. "
                  "Чтобы проверить оригинал, отправь фото файлом (📎 → Файл).\n\n" + header)
    return Report(header + "\n\n" + "\n\n".join(sections), location)


def _coordinates(tags: dict) -> tuple[float, float] | None:
    lat = _number(_first(tags, "Composite:GPSLatitude", "XMP:GPSLatitude", "EXIF:GPSLatitude"))
    lon = _number(_first(tags, "Composite:GPSLongitude", "XMP:GPSLongitude", "EXIF:GPSLongitude"))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _where(tags: dict, lat: float, lon: float) -> str:
    position = f"<code>{lat:.6f}, {lon:.6f}</code>"
    if accuracy := _number(tags.get("EXIF:GPSHPositioningError")):
        position += f" (точность ±{max(1, round(accuracy))} м)"
    lines = ["📍 <b>Где снято</b>", position]

    if altitude := _number(tags.get("Composite:GPSAltitude")):
        side = "над уровнем" if altitude > 0 else "ниже уровня"
        lines.append(f"Высота: {abs(altitude):.0f} м {side} моря")
    direction = _number(_first(tags, "EXIF:GPSImgDirection", "XMP:GPSImgDirection"))
    if direction is not None:
        lines.append(f"Камера смотрела на {DIRECTIONS[round(direction / 45) % 8]} ({direction:.0f}°)")
    if speed := _number(tags.get("EXIF:GPSSpeed")):
        speed *= {"M": 1.609, "N": 1.852}.get(tags.get("EXIF:GPSSpeedRef"), 1)  # K — уже км/ч
        if speed >= 1:
            lines.append(f"Скорость: {speed:.0f} км/ч")

    google = f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"
    yandex = f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"
    lines.append(f'<a href="{escape(google)}">Google Карты</a> · <a href="{escape(yandex)}">Яндекс Карты</a>')
    return "\n".join(lines)


def _when(tags: dict) -> str | None:
    taken = (
        _date(tags.get("Composite:SubSecDateTimeOriginal"))
        or _date(tags.get("EXIF:DateTimeOriginal"), tags.get("EXIF:OffsetTimeOriginal"))
        or _date(tags.get("QuickTime:CreationDate"))
        or _date(tags.get("XMP:DateTimeOriginal"))
        or _date(tags.get("EXIF:CreateDate"), tags.get("EXIF:OffsetTimeDigitized"))
        or _date(tags.get("XMP:DateCreated"))
        or _date(tags.get("QuickTime:CreateDate"), "Z")  # в видео это время по UTC
        or _date(tags.get("PDF:CreateDate"))
    )
    if not taken:
        return None
    lines = ["🕐 <b>Когда</b>", taken]
    modified = (
        _date(tags.get("Composite:SubSecModifyDate"))
        or _date(tags.get("EXIF:ModifyDate"), tags.get("EXIF:OffsetTime"))
        or _date(tags.get("XMP:ModifyDate"))
    )
    # Сравниваем без часового пояса: он бывает записан только у одной из дат
    if modified and modified.split(" (")[0] != taken.split(" (")[0]:
        lines.append(f"Изменено: {modified}")
    return "\n".join(lines)


def _device(tags: dict) -> str | None:
    lines = []
    make = _first(tags, "EXIF:Make", "QuickTime:Make", "XMP:Make")
    model = _first(tags, "EXIF:Model", "QuickTime:Model", "XMP:Model")
    if make and model and not str(model).lower().startswith(str(make).split()[0].lower()):
        lines.append(_esc(f"{make} {model}"))  # «Apple iPhone 13», но не «Canon Canon EOS R6»
    elif make or model:
        lines.append(_esc(model or make))

    for label, keys in (
        ("Объектив", ("EXIF:LensModel", "XMP:LensModel", "MakerNotes:LensModel")),
        ("ПО", ("EXIF:Software", "XMP:CreatorTool", "QuickTime:Software")),
        ("Серийный номер", ("EXIF:SerialNumber", "MakerNotes:SerialNumber",
                            "MakerNotes:InternalSerialNumber", "XMP:SerialNumber")),
        ("Серийный номер объектива", ("EXIF:LensSerialNumber", "MakerNotes:LensSerialNumber",
                                      "XMP:LensSerialNumber")),
    ):
        if value := _first(tags, *keys):
            lines.append(f"{label}: {_esc(value)}")
    return "\n".join(["📱 <b>Устройство</b>", *lines]) if lines else None


def _author(tags: dict) -> str | None:
    lines = []
    for label, keys in (
        ("Имя", ("EXIF:Artist", "XMP:Creator", "IPTC:By-line", "EXIF:XPAuthor", "PDF:Author")),
        ("Владелец камеры", ("EXIF:OwnerName", "MakerNotes:OwnerName")),
        ("Копирайт", ("EXIF:Copyright", "XMP:Rights", "IPTC:CopyrightNotice")),
    ):
        if value := _first(tags, *keys):
            lines.append(f"{label}: {_esc(value)}")
    return "\n".join(["👤 <b>Автор</b>", *lines]) if lines else None


def _shot(tags: dict) -> str | None:
    parts = []
    if exposure := _number(tags.get("EXIF:ExposureTime")):
        parts.append(f"1/{round(1 / exposure)} с" if exposure < 0.25 else f"{exposure:g} с")
    if f_number := _number(tags.get("EXIF:FNumber")):
        parts.append(f"f/{f_number:g}")
    if iso := _number(tags.get("EXIF:ISO")):
        parts.append(f"ISO {iso:g}")
    if focal := _number(tags.get("EXIF:FocalLength")):
        equivalent = _number(tags.get("EXIF:FocalLengthIn35mmFormat"))
        if equivalent and round(equivalent) != round(focal):
            parts.append(f"{focal:g} мм (эквивалент {equivalent:g} мм)")
        else:
            parts.append(f"{focal:g} мм")
    return "📷 <b>Параметры съёмки</b>\n" + " · ".join(parts) if parts else None


def _file(tags: dict, size: int) -> str:
    parts = [str(tags.get("File:FileType", "неизвестный формат"))]
    if dimensions := tags.get("Composite:ImageSize"):
        parts.append(str(dimensions).replace(" ", "×"))
    if size < 1024:
        parts.append(f"{size} Б")
    elif size < 1024 ** 2:
        parts.append(f"{size / 1024:.0f} КБ")
    else:
        parts.append(f"{size / 1024 ** 2:.1f} МБ")
    return "🗂 <b>Файл</b>\n" + _esc(" · ".join(parts))


def _date(value, zone: str | None = None) -> str | None:
    """«2026:09:15 18:42:07+03:00» → «15.09.2026 в 18:42:07 (UTC+03:00)»."""
    match = DATE_RE.match(str(value or ""))
    if not match or match[1] == "0000":
        return None
    year, month, day, hours, minutes, seconds, own_zone = match.groups()
    text = f"{day}.{month}.{year} в {hours}:{minutes}:{seconds}"
    zone = own_zone or zone
    if zone == "Z":
        text += " (UTC)"
    elif zone and re.fullmatch(r"[+-]\d\d:\d\d", str(zone)):
        text += f" (UTC{zone})"
    return text


def _first(tags: dict, *keys: str):
    """Первое непустое значение среди перечисленных тегов."""
    for key in keys:
        value = tags.get(key)
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        if value is not None and str(value).strip():
            return value
    return None


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _esc(value, limit: int = 200) -> str:
    text = str(value).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return escape(text)
