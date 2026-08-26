#!/usr/bin/env python3
"""
Синхронизация каталога Юникар.

Источники:
1. Avito API — актуальные объявления, цена, наличие, ссылка.
2. 12981.xml — фотографии, описание и расширенные характеристики.

Переменные окружения:
    AVITO_CLIENT_ID
    AVITO_CLIENT_SECRET

Результат:
    cars-data.js
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import requests


BASE_URL = "https://api.avito.ru"
TOKEN_URL = f"{BASE_URL}/token"
ITEMS_URL = f"{BASE_URL}/core/v1/items"
ACCOUNT_URL = f"{BASE_URL}/core/v1/accounts/self"
ITEM_DETAILS_URL = f"{BASE_URL}/core/v1/accounts/{{user_id}}/items/{{item_id}}/"

XML_FILE = Path("12981.xml")
MEDIA_FILE = Path("cars-media.json")
OUTPUT_FILE = Path("cars-data.js")
IMAGE_DIR = Path("car-images")

TIMEOUT = 30
PAGE_SIZE = 100


class AvitoError(RuntimeError):
    pass


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AvitoError(f"Не задана переменная окружения {name}")
    return value


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        data=data,
        timeout=TIMEOUT,
    )

    if response.status_code >= 400:
        raise AvitoError(
            f"Avito API вернул HTTP {response.status_code} для {url}: "
            f"{response.text[:1000]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise AvitoError(f"Avito API вернул не JSON для {url}") from exc

    if not isinstance(payload, dict):
        raise AvitoError(f"Неожиданный формат ответа Avito API для {url}")

    return payload


def get_access_token(client_id: str, client_secret: str) -> str:
    payload = request_json(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )

    token = str(payload.get("access_token", "")).strip()
    if not token:
        raise AvitoError(f"В ответе отсутствует access_token: {payload}")

    return token


def bearer(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "UnicarWebsiteSync/2.0",
    }


def get_account(token: str) -> dict[str, Any]:
    return request_json("GET", ACCOUNT_URL, headers=bearer(token))


def get_item_details(token: str, user_id: int, item_id: int) -> dict[str, Any]:
    """Получает детальную карточку объявления Avito, включая изображения."""
    url = ITEM_DETAILS_URL.format(user_id=user_id, item_id=item_id)
    return request_json("GET", url, headers=bearer(token))


def extract_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for value in (
        payload.get("resources"),
        payload.get("items"),
        payload.get("result"),
        payload.get("data"),
    ):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

        if isinstance(value, dict):
            for key in ("items", "resources", "result"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]

    return []


def get_active_items(token: str) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = request_json(
            "GET",
            ITEMS_URL,
            headers=bearer(token),
            params={
                "status": "active",
                "page": page,
                "per_page": PAGE_SIZE,
            },
        )

        batch = extract_list(payload)
        if not batch:
            break

        all_items.extend(batch)

        if len(batch) < PAGE_SIZE:
            break

        page += 1
        if page > 100:
            raise AvitoError("Остановлена подозрительно длинная пагинация Avito API")

        time.sleep(0.2)

    return all_items


def first_value(obj: dict[str, Any], *paths: str, default: Any = "") -> Any:
    for path in paths:
        current: Any = obj
        ok = True

        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break

        if ok and current not in (None, "", []):
            return current

    return default


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else 0


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_token(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_param(item: dict[str, Any], names: tuple[str, ...]) -> str:
    raw = first_value(item, "params", "parameters", "attributes", default=[])

    if isinstance(raw, dict):
        for name in names:
            for key, value in raw.items():
                if normalize_token(key) == normalize_token(name):
                    return str(value)

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue

            label = str(
                first_value(entry, "name", "title", "label", "slug", default="")
            )

            if any(normalize_token(name) == normalize_token(label) for name in names):
                return str(
                    first_value(entry, "value", "value_title", "text", default="")
                )

    return ""


def parse_title(title: str) -> tuple[str, str, int, int, str, str]:
    title = re.sub(r"\s+", " ", title).strip()

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    year = int(year_match.group(1)) if year_match else 0

    mileage_match = re.search(
        r"(\d{1,3}(?:\s\d{3})+|\d+)\s*км\b",
        title,
        flags=re.I,
    )
    mileage = (
        int(re.sub(r"\D", "", mileage_match.group(1)))
        if mileage_match
        else 0
    )

    transmission = ""
    if re.search(r"\bMT\b", title, flags=re.I):
        transmission = "механика"
    elif re.search(r"\bAT\b", title, flags=re.I):
        transmission = "автомат"
    elif re.search(r"\bCVT\b", title, flags=re.I):
        transmission = "вариатор"
    elif re.search(r"\bAMT\b", title, flags=re.I):
        transmission = "робот"

    engine_match = re.search(r"\b(\d+[.,]\d+)\b", title)
    engine = engine_match.group(1).replace(",", ".") if engine_match else ""

    clean = title
    clean = re.sub(
        r"(\d{1,3}(?:\s\d{3})+|\d+)\s*км\b",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\b(19\d{2}|20\d{2})\b", "", clean)
    clean = re.sub(r"\b(?:MT|AT|CVT|AMT)\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d+[.,]\d+\b", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" ,.-")

    parts = clean.split()
    brand = parts[0] if parts else ""
    model = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

    return brand, model, year, mileage, transmission, engine


def normalize_transmission(value: str) -> str:
    value_normalized = normalize_token(value)

    if not value_normalized:
        return ""

    if any(term in value_normalized for term in ("механ", "мкпп")) or value_normalized == "mt":
        return "механика"

    if any(term in value_normalized for term in ("автомат", "акпп")) or value_normalized == "at":
        return "автомат"

    if any(term in value_normalized for term in ("робот", "dsg", "amt")):
        return "робот"

    if any(term in value_normalized for term in ("вариатор", "cvt")):
        return "вариатор"

    return str(value).strip()


def normalize_drive(value: str) -> str:
    value_normalized = normalize_token(value)

    if "полн" in value_normalized or "4wd" in value_normalized or "awd" in value_normalized:
        return "полный"

    if "задн" in value_normalized:
        return "задний"

    if "передн" in value_normalized:
        return "передний"

    return str(value).strip()



def _collect_http_urls(value: Any) -> list[str]:
    """Собирает HTTP(S)-ссылки из узла JSON, сохраняя порядок."""
    result: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            url = node.strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                result.append(url)
            return

        if isinstance(node, list):
            for child in node:
                walk(child)
            return

        if isinstance(node, dict):
            # В объектах Avito у фото размеры могут быть ключами:
            # 640x480, 1280x960, original и т.п.
            preferred_keys = (
                "original",
                "1280x960",
                "1024x768",
                "640x480",
                "url",
                "src",
                "href",
            )

            used: set[str] = set()
            for key in preferred_keys:
                if key in node:
                    used.add(key)
                    walk(node[key])

            for key, child in node.items():
                if key not in used:
                    walk(child)

    walk(value)
    return result


def extract_api_images(item: dict[str, Any]) -> list[str]:
    """
    Извлекает фотографии из детального ответа Avito API.
    Поддерживает разные варианты структуры ответа.
    """
    candidates: list[Any] = []

    for path in (
        "images",
        "photos",
        "pictures",
        "image_urls",
        "photo_urls",
        "resources.images",
        "item.images",
        "item.photos",
        "data.images",
        "data.photos",
    ):
        value = first_value(item, path, default=None)
        if value not in (None, "", []):
            candidates.append(value)

    urls: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        for url in _collect_http_urls(candidate):
            if url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def normalize_api_item(item: dict[str, Any]) -> dict[str, Any]:
    item_id = to_int(first_value(item, "id", "item_id", "avito_id"))
    title = str(first_value(item, "title", "name", default="")).strip()

    (
        parsed_brand,
        parsed_model,
        parsed_year,
        parsed_mileage,
        parsed_transmission,
        parsed_engine,
    ) = parse_title(title)

    brand = str(
        first_value(item, "make", "brand", "params.make", default="")
        or extract_param(item, ("Марка", "make", "brand"))
        or parsed_brand
    ).strip()

    model = str(
        first_value(item, "model", "params.model", default="")
        or extract_param(item, ("Модель", "model"))
        or parsed_model
    ).strip()

    year = to_int(
        first_value(item, "year", "params.year", default="")
        or extract_param(item, ("Год выпуска", "Год", "year"))
        or parsed_year
    )

    mileage = to_int(
        first_value(item, "mileage", "kilometrage", "params.mileage", default="")
        or extract_param(item, ("Пробег", "mileage", "kilometrage"))
        or parsed_mileage
    )

    transmission_raw = str(
        first_value(item, "transmission", "params.transmission", default="")
        or extract_param(item, ("Коробка передач", "КПП", "transmission"))
        or parsed_transmission
    )

    price = to_int(
        first_value(item, "price", "price.value", "price_amount", default=0)
    )

    return {
        "id": item_id,
        "title": title,
        "brand": brand,
        "model": model,
        "year": year,
        "price": price,
        "mileage": mileage,
        "engine": parsed_engine,
        "transmission": normalize_transmission(transmission_raw),
        "drive": "",
        "owners": "",
        "bodyType": "",
        "color": "",
        "fuel": "",
        "power": "",
        "vin": "",
        "description": "",
        "images": extract_api_images(item),
        "video": "",
        "avitoUrl": str(
            first_value(item, "url", "item_url", "link", default="")
        ).strip(),
    }


def is_car(car: dict[str, Any]) -> bool:
    url = str(car.get("avitoUrl", "")).lower()

    return (
        bool(car.get("id"))
        and "/avtomobili/" in url
        and int(car.get("year", 0)) > 0
        and int(car.get("price", 0)) > 0
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""

    return clean_text("".join(element.itertext()))


def build_child_map(element: ET.Element) -> dict[str, list[ET.Element]]:
    mapping: dict[str, list[ET.Element]] = {}

    for child in element.iter():
        if child is element:
            continue
        mapping.setdefault(local_name(child.tag), []).append(child)

    return mapping


def xml_value(
    child_map: dict[str, list[ET.Element]],
    aliases: Iterable[str],
) -> str:
    for alias in aliases:
        elements = child_map.get(alias.lower(), [])
        for element in elements:
            value = element_text(element)
            if value:
                return value

    return ""


def extract_xml_images(element: ET.Element) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    for child in element.iter():
        name = local_name(child.tag)

        is_image_tag = name in {
            "image",
            "photo",
            "picture",
            "imageurl",
            "image_url",
            "photo_url",
        }

        if not is_image_tag and name not in {"images", "photos", "pictures"}:
            continue

        candidates = [
            child.attrib.get("url"),
            child.attrib.get("src"),
            child.attrib.get("href"),
            (child.text or "").strip(),
        ]

        for candidate in candidates:
            if not candidate:
                continue

            url = html.unescape(str(candidate)).strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                images.append(url)
                seen.add(url)

    return images


@dataclass
class XmlCar:
    brand: str
    model: str
    year: int
    mileage: int
    price: int
    engine: str
    transmission: str
    drive: str
    owners: str
    body_type: str
    color: str
    fuel: str
    power: str
    vin: str
    description: str
    images: list[str]
    video: str
    source_id: str


def looks_like_vehicle_record(element: ET.Element) -> bool:
    direct_names = {local_name(child.tag) for child in list(element)}

    has_brand = bool(
        direct_names
        & {
            "make",
            "brand",
            "mark",
            "vehiclemake",
        }
    )
    has_model = "model" in direct_names or "vehiclemodel" in direct_names
    has_year = bool(direct_names & {"year", "yearofmanufacture", "productionyear"})
    has_price = "price" in direct_names

    return has_brand and has_model and has_year and has_price


def candidate_xml_records(root: ET.Element) -> list[ET.Element]:
    records = [element for element in root.iter() if looks_like_vehicle_record(element)]

    if records:
        return records

    # Резервный вариант для фидов Avito с типовым тегом Ad.
    return [
        element
        for element in root.iter()
        if local_name(element.tag) in {"ad", "item", "offer", "vehicle"}
    ]


def parse_xml_catalog(path: Path) -> list[XmlCar]:
    if not path.exists():
        print(f"Предупреждение: {path} не найден, карточки будут без фото и описания")
        return []

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise AvitoError(f"Не удалось разобрать XML-файл {path}: {exc}") from exc

    root = tree.getroot()
    records = candidate_xml_records(root)
    result: list[XmlCar] = []

    for record in records:
        child_map = build_child_map(record)

        brand = xml_value(
            child_map,
            ("make", "brand", "mark", "vehiclemake"),
        )
        model = xml_value(
            child_map,
            ("model", "vehiclemodel"),
        )
        year = to_int(
            xml_value(
                child_map,
                ("year", "yearofmanufacture", "productionyear"),
            )
        )
        price = to_int(xml_value(child_map, ("price",)))
        mileage = to_int(
            xml_value(
                child_map,
                ("kilometrage", "mileage", "run"),
            )
        )

        if not brand or not model or not year:
            continue

        engine = xml_value(
            child_map,
            (
                "engine",
                "enginedisplacement",
                "modification",
                "generation",
            ),
        )
        modification = xml_value(
            child_map,
            ("modification", "enginemodification"),
        )

        if modification:
            engine_match = re.search(r"\b(\d+[.,]\d+)\b", modification)
            if engine_match:
                engine = engine_match.group(1).replace(",", ".")

        power = xml_value(
            child_map,
            ("power", "horsepower", "enginepower"),
        )
        if not power and modification:
            power_match = re.search(r"(\d+)\s*л\.?\s*с", modification, flags=re.I)
            if power_match:
                power = power_match.group(1)

        transmission = normalize_transmission(
            xml_value(
                child_map,
                ("transmission", "gearbox", "transmissiontype"),
            )
        )
        drive = normalize_drive(
            xml_value(
                child_map,
                ("drive", "drivetype"),
            )
        )
        owners = xml_value(
            child_map,
            ("owners", "ownerscount", "numberofowners"),
        )
        body_type = xml_value(
            child_map,
            ("bodytype", "body", "vehicletype"),
        )
        color = xml_value(child_map, ("color",))
        fuel = xml_value(
            child_map,
            ("fueltype", "fuel"),
        )
        vin = xml_value(child_map, ("vin",))
        description = xml_value(
            child_map,
            ("description", "text", "fulldescription"),
        )
        video = xml_value(
            child_map,
            ("video", "videourl", "video_url"),
        )
        source_id = xml_value(
            child_map,
            ("id", "avitoid", "adid", "externalid"),
        )

        result.append(
            XmlCar(
                brand=brand.strip(),
                model=model.strip(),
                year=year,
                mileage=mileage,
                price=price,
                engine=engine.strip(),
                transmission=transmission,
                drive=drive,
                owners=owners.strip(),
                body_type=body_type.strip(),
                color=color.strip(),
                fuel=fuel.strip(),
                power=power.strip(),
                vin=vin.strip(),
                description=description,
                images=extract_xml_images(record),
                video=video.strip(),
                source_id=source_id.strip(),
            )
        )

    print(f"Автомобилей разобрано из XML: {len(result)}")
    return result


def compact_token(value: Any) -> str:
    """Нормализованная строка без пробелов для устойчивого сравнения."""
    return normalize_token(value).replace(" ", "")


def normalized_words(value: Any) -> set[str]:
    return {
        word
        for word in normalize_token(value).split()
        if len(word) > 1
    }


def extract_engine_number(value: Any) -> float | None:
    match = re.search(r"\b(\d+[.,]\d+)\b", str(value or ""))
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def model_similarity(api_model: str, xml_model: str) -> float:
    """
    Возвращает похожесть модели от 0 до 1.
    Учитывает варианты вроде:
      Duster ↔ Duster 2.0 4WD
      Golf Plus ↔ GolfPlus
      EX25 ↔ EX 25
    """
    api_normalized = normalize_token(api_model)
    xml_normalized = normalize_token(xml_model)

    if not api_normalized or not xml_normalized:
        return 0.0

    api_compact = compact_token(api_model)
    xml_compact = compact_token(xml_model)

    if api_compact == xml_compact:
        return 1.0

    if api_compact in xml_compact or xml_compact in api_compact:
        shorter = min(len(api_compact), len(xml_compact))
        longer = max(len(api_compact), len(xml_compact))
        return max(0.88, shorter / longer)

    api_words = normalized_words(api_model)
    xml_words = normalized_words(xml_model)

    word_score = 0.0
    if api_words and xml_words:
        intersection = len(api_words & xml_words)
        union = len(api_words | xml_words)
        word_score = intersection / union if union else 0.0

    sequence_score = SequenceMatcher(
        None,
        api_compact,
        xml_compact,
    ).ratio()

    return max(word_score, sequence_score)


def source_id_matches(api_car: dict[str, Any], xml_car: XmlCar) -> bool:
    """
    Ищет точное совпадение по Avito ID, если XML хранит ID или ссылку.
    """
    api_id = str(api_car.get("id", "")).strip()
    source = str(xml_car.source_id or "").strip()

    if not api_id or not source:
        return False

    return api_id == source or api_id in source


def match_score(api_car: dict[str, Any], xml_car: XmlCar) -> tuple[int, list[str]]:
    """
    Возвращает балл и объяснение.

    Жёсткие ограничения:
    - марка должна совпасть;
    - модель должна быть достаточно похожа;
    - год обычно совпадает точно, допускается ±1 только при очень близком пробеге.
    """
    reasons: list[str] = []

    if source_id_matches(api_car, xml_car):
        return 10_000, ["точный Avito ID"]

    api_brand = compact_token(api_car.get("brand", ""))
    xml_brand = compact_token(xml_car.brand)

    if not api_brand or api_brand != xml_brand:
        return -10_000, ["марка не совпала"]

    similarity = model_similarity(
        str(api_car.get("model", "")),
        xml_car.model,
    )

    if similarity < 0.58:
        return -10_000, [f"модель слишком отличается: {similarity:.2f}"]

    score = 200
    reasons.append(f"марка совпала")
    reasons.append(f"модель {similarity:.2f}")
    score += int(similarity * 220)

    api_year = int(api_car.get("year", 0))
    year_diff = abs(api_year - int(xml_car.year))

    api_mileage = int(api_car.get("mileage", 0))
    xml_mileage = int(xml_car.mileage or 0)
    mileage_diff = (
        abs(api_mileage - xml_mileage)
        if api_mileage and xml_mileage
        else None
    )

    if year_diff == 0:
        score += 220
        reasons.append("год совпал")
    elif year_diff == 1 and mileage_diff is not None and mileage_diff <= 3_000:
        score += 50
        reasons.append("год отличается на 1, пробег близкий")
    else:
        return -10_000, [f"год не совпал: {api_year}/{xml_car.year}"]

    if mileage_diff is not None:
        if mileage_diff == 0:
            score += 300
            reasons.append("пробег точный")
        elif mileage_diff <= 100:
            score += 280
            reasons.append(f"пробег ±{mileage_diff}")
        elif mileage_diff <= 500:
            score += 250
            reasons.append(f"пробег ±{mileage_diff}")
        elif mileage_diff <= 2_000:
            score += 200
            reasons.append(f"пробег ±{mileage_diff}")
        elif mileage_diff <= 5_000:
            score += 130
            reasons.append(f"пробег ±{mileage_diff}")
        elif mileage_diff <= 15_000:
            score += 50
            reasons.append(f"пробег ±{mileage_diff}")
        else:
            score -= min(180, mileage_diff // 1_000)
            reasons.append(f"пробег сильно отличается: {mileage_diff}")
    else:
        reasons.append("пробег в одном источнике отсутствует")

    api_engine = extract_engine_number(api_car.get("engine", ""))
    xml_engine = extract_engine_number(xml_car.engine)

    if api_engine is not None and xml_engine is not None:
        engine_diff = abs(api_engine - xml_engine)
        if engine_diff < 0.05:
            score += 100
            reasons.append("двигатель совпал")
        elif engine_diff <= 0.2:
            score += 30
            reasons.append("двигатель близкий")
        else:
            score -= 100
            reasons.append("двигатель отличается")

    api_transmission = normalize_transmission(
        str(api_car.get("transmission", ""))
    )
    xml_transmission = normalize_transmission(xml_car.transmission)

    if api_transmission and xml_transmission:
        if api_transmission == xml_transmission:
            score += 80
            reasons.append("КПП совпала")
        else:
            score -= 160
            reasons.append("КПП отличается")

    api_price = int(api_car.get("price", 0))
    xml_price = int(xml_car.price or 0)

    if api_price and xml_price:
        price_diff = abs(api_price - xml_price)
        price_ratio = price_diff / max(api_price, xml_price)

        if price_diff == 0:
            score += 60
            reasons.append("цена точная")
        elif price_ratio <= 0.03:
            score += 45
            reasons.append("цена близкая")
        elif price_ratio <= 0.10:
            score += 20
            reasons.append("цена отличается до 10%")
        elif price_ratio > 0.35:
            score -= 80
            reasons.append("цена сильно отличается")

    # Фото и описание делают запись предпочтительнее при равных характеристиках.
    if xml_car.images:
        score += min(60, len(xml_car.images) * 5)
        reasons.append(f"фото {len(xml_car.images)}")

    if xml_car.description:
        score += 20
        reasons.append("есть описание")

    return score, reasons


def find_xml_match(
    api_car: dict[str, Any],
    xml_cars: list[XmlCar],
) -> tuple[XmlCar | None, int, list[str]]:
    candidates: list[tuple[int, XmlCar, list[str]]] = []

    for xml_car in xml_cars:
        score, reasons = match_score(api_car, xml_car)
        if score > -10_000:
            candidates.append((score, xml_car, reasons))

    if not candidates:
        return None, 0, []

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_car, best_reasons = candidates[0]

    # Защита от сомнительных совпадений.
    if best_score < 500:
        return None, best_score, best_reasons

    # Если два кандидата почти равны, не подставляем чужое фото.
    if len(candidates) > 1:
        second_score = candidates[1][0]
        if best_score - second_score < 35 and best_score < 850:
            return None, best_score, [
                *best_reasons,
                f"неоднозначно: второй кандидат {second_score}",
            ]

    return best_car, best_score, best_reasons

def merge_car(api_car: dict[str, Any], xml_car: XmlCar | None) -> dict[str, Any]:
    merged = dict(api_car)

    if xml_car is None:
        return merged

    merged["engine"] = xml_car.engine or merged.get("engine", "")
    merged["transmission"] = (
        merged.get("transmission")
        or xml_car.transmission
    )
    merged["drive"] = xml_car.drive
    merged["owners"] = xml_car.owners
    merged["bodyType"] = xml_car.body_type
    merged["color"] = xml_car.color
    merged["fuel"] = xml_car.fuel
    merged["power"] = xml_car.power
    merged["vin"] = xml_car.vin
    merged["description"] = xml_car.description
    merged["images"] = xml_car.images
    merged["video"] = xml_car.video

    return merged


def load_manual_media(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        print(f"Предупреждение: {path} не найден")
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AvitoError(f"Не удалось прочитать {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise AvitoError(f"{path} должен содержать JSON-объект")

    result: dict[str, dict[str, Any]] = {}
    for item_id, media in payload.items():
        if not isinstance(media, dict):
            continue

        normalized = dict(media)
        raw_images = normalized.get("images", [])
        if not isinstance(raw_images, list):
            raw_images = []

        normalized["images"] = [
            str(url).strip()
            for url in raw_images
            if str(url).strip().startswith(("http://", "https://", "/"))
        ]
        normalized["description"] = clean_text(normalized.get("description", ""))
        result[str(item_id)] = normalized

    print(f"Ручных карточек в {path}: {len(result)}")
    return result


def load_previous_cars(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8")
        match = re.search(r"const\s+cars\s*=\s*(\[.*\])\s*;\s*$", content, flags=re.S)
        if not match:
            return {}

        payload = json.loads(match.group(1))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for car in payload:
        if isinstance(car, dict) and car.get("id"):
            result[str(car["id"])] = car

    print(f"Предыдущих карточек сохранено: {len(result)}")
    return result


def apply_media_fields(
    car: dict[str, Any],
    source: dict[str, Any] | None,
    *,
    overwrite: bool,
) -> dict[str, Any]:
    if not source:
        return car

    fields = (
        "engine",
        "drive",
        "owners",
        "bodyType",
        "color",
        "fuel",
        "power",
        "vin",
        "description",
        "images",
        "video",
    )

    for field in fields:
        value = source.get(field)
        if value in (None, "", []):
            continue

        if overwrite or car.get(field) in (None, "", []):
            car[field] = value

    return car


def merge_all_sources(
    api_car: dict[str, Any],
    xml_car: XmlCar | None,
    previous: dict[str, dict[str, Any]],
    manual: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged = merge_car(api_car, xml_car)
    item_id = str(api_car["id"])

    merged = apply_media_fields(
        merged,
        previous.get(item_id),
        overwrite=False,
    )
    merged = apply_media_fields(
        merged,
        manual.get(item_id),
        overwrite=True,
    )

    return merged

def natural_sort_key(path: Path) -> list[Any]:
    """Сортировка 001.jpg, 002.jpg, 010.jpg в правильном порядке."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def find_local_images(item_id: int) -> list[str]:
    """
    Ищет все изображения в car-images/<Avito ID>/.
    Поддерживает любые имена: 001.jpg, front.webp, salon.png и т.д.
    """
    folder = IMAGE_DIR / str(item_id)
    if not folder.exists() or not folder.is_dir():
        return []

    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in allowed_extensions
    ]
    files.sort(key=natural_sort_key)

    return [
        f"/car-images/{item_id}/{path.name}"
        for path in files
    ]



def image_extension(url: str, content_type: str = "") -> str:
    """Определяет расширение изображения по URL или Content-Type."""
    path_part = url.split("?", 1)[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
        if path_part.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext

    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "avif" in content_type:
        return ".avif"
    return ".jpg"


def download_external_images(car: dict[str, Any]) -> dict[str, Any]:
    """
    Автоматически скачивает внешние фотографии автомобиля в
    car-images/<Avito ID>/.

    Уже существующие локальные фото не трогает.
    """
    item_id = int(car.get("id", 0))
    if not item_id:
        return car

    if find_local_images(item_id):
        return car

    raw_images = car.get("images", [])
    if not isinstance(raw_images, list):
        return car

    urls = [
        str(url).strip()
        for url in raw_images
        if str(url).strip().startswith(("http://", "https://"))
    ]
    if not urls:
        return car

    folder = IMAGE_DIR / str(item_id)
    folder.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; UnicarWebsiteSync/2.0)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://www.avito.ru/",
    })

    downloaded = 0

    for index, url in enumerate(urls, start=1):
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if content_type and not content_type.lower().startswith("image/"):
                print(f"Фото пропущено {item_id}: {content_type} — {url}")
                continue

            if not response.content:
                print(f"Фото пропущено {item_id}: пустой ответ — {url}")
                continue

            ext = image_extension(url, content_type)
            target = folder / f"{index:03d}{ext}"
            target.write_bytes(response.content)
            downloaded += 1

        except requests.RequestException as exc:
            print(f"Не удалось скачать фото {item_id}: {url} — {exc}")

    if downloaded:
        print(f"Фото скачаны автоматически: {item_id} — {downloaded} шт.")
    else:
        try:
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    return car


def apply_local_images(car: dict[str, Any]) -> dict[str, Any]:
    """
    Локальные фотографии из GitHub имеют высший приоритет.
    Они не зависят от Avito, XML и внешних ссылок.
    """
    item_id = int(car.get("id", 0))
    if not item_id:
        return car

    local_images = find_local_images(item_id)
    if local_images:
        car["images"] = local_images
        print(
            f"Локальные фото найдены: {item_id} — {len(local_images)} шт."
        )

    return car

def write_cars_js(cars: list[dict[str, Any]]) -> None:
    content = (
        "// ========== АВТОМОБИЛИ ИЗ AVITO API + XML ==========\n"
        "// Файл обновляется автоматически GitHub Actions. Не редактируйте вручную.\n\n"
        "const cars = "
        + json.dumps(cars, ensure_ascii=False, indent=4)
        + ";\n"
    )

    OUTPUT_FILE.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    try:
        client_id = require_env("AVITO_CLIENT_ID")
        client_secret = require_env("AVITO_CLIENT_SECRET")

        token = get_access_token(client_id, client_secret)
        account = get_account(token)

        print(
            "Avito account:",
            first_value(account, "id", "user_id", default="не определён"),
            first_value(account, "name", default=""),
        )

        raw_items = get_active_items(token)

        # Сначала определяем автомобили по краткому списку объявлений.
        summary_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_item in raw_items:
            summary_car = normalize_api_item(raw_item)
            if is_car(summary_car):
                summary_pairs.append((raw_item, summary_car))

        user_id = to_int(first_value(account, "id", "user_id", default=0))
        if not user_id:
            raise AvitoError("Не удалось определить ID аккаунта Avito")

        # Для каждого автомобиля получаем детальную карточку.
        # Именно детальный метод Avito содержит массив изображений.
        api_cars: list[dict[str, Any]] = []
        detailed_count = 0

        for raw_item, summary_car in summary_pairs:
            item_id = int(summary_car["id"])

            try:
                details = get_item_details(token, user_id, item_id)

                combined = dict(raw_item)

                # Некоторые версии API оборачивают карточку в item/data/resource.
                for wrapper in ("item", "data", "resource"):
                    nested = details.get(wrapper)
                    if isinstance(nested, dict):
                        combined.update(nested)

                combined.update(details)

                # Сохраняем ссылку из списка, если детальный ответ её не вернул.
                if not first_value(combined, "url", "item_url", "link", default=""):
                    combined["url"] = summary_car.get("avitoUrl", "")

                detailed_car = normalize_api_item(combined)

                # На случай нестандартной структуры ответа извлекаем фото
                # непосредственно из полного JSON детальной карточки.
                detail_images = extract_api_images(details)
                if detail_images:
                    detailed_car["images"] = detail_images

                api_cars.append(detailed_car)
                detailed_count += 1

                print(
                    f"Avito detail: {item_id} — "
                    f"фото {len(detailed_car.get('images', []))} шт."
                )

            except (AvitoError, requests.RequestException) as exc:
                print(
                    f"Предупреждение: детальная карточка Avito {item_id} "
                    f"не получена — {exc}"
                )
                api_cars.append(summary_car)

            time.sleep(0.15)

        xml_cars = parse_xml_catalog(XML_FILE)
        manual_media = load_manual_media(MEDIA_FILE)
        previous_cars = load_previous_cars(OUTPUT_FILE)

        merged_cars: list[dict[str, Any]] = []
        matched_count = 0
        manual_count = 0

        for api_car in api_cars:
            xml_match, xml_score, xml_reasons = find_xml_match(
                api_car,
                xml_cars,
            )
            item_id = str(api_car["id"])

            if xml_match is not None:
                matched_count += 1
                print(
                    "XML найден:",
                    f"{api_car['brand']} {api_car['model']} {api_car['year']}",
                    "→",
                    f"{xml_match.brand} {xml_match.model} {xml_match.year}",
                    f"— балл: {xml_score}",
                    f"— фото: {len(xml_match.images)}",
                    f"— причины: {', '.join(xml_reasons)}",
                )
            else:
                print(
                    "XML не найден:",
                    f"{api_car['brand']} {api_car['model']} {api_car['year']}",
                    f"{api_car['mileage']} км",
                    f"— лучший балл: {xml_score}",
                    f"— причины: {', '.join(xml_reasons) or 'нет кандидатов'}",
                )

            if item_id in manual_media:
                manual_count += 1
                print("Ручные данные применены:", item_id)

            merged = merge_all_sources(
                api_car,
                xml_match,
                previous_cars,
                manual_media,
            )

            # Если локальных фото ещё нет, автоматически скачиваем
            # внешние фотографии из XML/ручных данных в car-images/<Avito ID>.
            merged = download_external_images(merged)

            # Локальные фотографии из car-images/<Avito ID> имеют
            # высший приоритет и не требуют перечисления файлов вручную.
            merged = apply_local_images(merged)

            merged_cars.append(merged)

        merged_cars.sort(
            key=lambda car: (
                int(car.get("year", 0)),
                int(car.get("price", 0)),
            ),
            reverse=True,
        )

        write_cars_js(merged_cars)

        print(f"Активных объявлений Avito получено: {len(raw_items)}")
        print(f"Автомобилей после фильтра: {len(api_cars)}")
        print(f"Детальных карточек Avito получено: {detailed_count}")
        print(f"Карточек дополнено из XML: {matched_count}")
        print(f"Карточек дополнено вручную: {manual_count}")
        print(f"Результат записан в {OUTPUT_FILE}")

        if raw_items and not api_cars:
            raise AvitoError(
                "API вернул объявления, но ни одно не прошло фильтр автомобилей."
            )

        return 0

    except (AvitoError, requests.RequestException, OSError) as exc:
        print(f"Ошибка синхронизации: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
