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
from pathlib import Path
from typing import Any, Iterable

import requests


BASE_URL = "https://api.avito.ru"
TOKEN_URL = f"{BASE_URL}/token"
ITEMS_URL = f"{BASE_URL}/core/v1/items"
ACCOUNT_URL = f"{BASE_URL}/core/v1/accounts/self"

XML_FILE = Path("12981.xml")
MEDIA_FILE = Path("cars-media.json")
OUTPUT_FILE = Path("cars-data.js")

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
        "images": [],
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


def model_matches(api_model: str, xml_model: str) -> bool:
    api_value = normalize_token(api_model)
    xml_value_normalized = normalize_token(xml_model)

    if not api_value or not xml_value_normalized:
        return False

    return (
        api_value == xml_value_normalized
        or api_value in xml_value_normalized
        or xml_value_normalized in api_value
    )


def match_score(api_car: dict[str, Any], xml_car: XmlCar) -> int:
    if normalize_token(api_car["brand"]) != normalize_token(xml_car.brand):
        return -10_000

    if not model_matches(str(api_car["model"]), xml_car.model):
        return -10_000

    if int(api_car["year"]) != xml_car.year:
        return -10_000

    score = 100

    api_mileage = int(api_car.get("mileage", 0))
    if api_mileage and xml_car.mileage:
        mileage_diff = abs(api_mileage - xml_car.mileage)

        if mileage_diff == 0:
            score += 100
        elif mileage_diff <= 500:
            score += 80
        elif mileage_diff <= 2_000:
            score += 60
        elif mileage_diff <= 10_000:
            score += 20
        else:
            score -= min(80, mileage_diff // 5_000)

    api_price = int(api_car.get("price", 0))
    if api_price and xml_car.price:
        price_diff = abs(api_price - xml_car.price)

        if price_diff == 0:
            score += 30
        elif price_diff <= 25_000:
            score += 20
        elif price_diff <= 100_000:
            score += 5

    return score


def find_xml_match(
    api_car: dict[str, Any],
    xml_cars: list[XmlCar],
) -> XmlCar | None:
    matches = [
        (match_score(api_car, xml_car), xml_car)
        for xml_car in xml_cars
    ]

    matches = [pair for pair in matches if pair[0] >= 100]
    if not matches:
        return None

    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


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
        api_cars = [normalize_api_item(item) for item in raw_items]
        api_cars = [car for car in api_cars if is_car(car)]

        xml_cars = parse_xml_catalog(XML_FILE)
        manual_media = load_manual_media(MEDIA_FILE)
        previous_cars = load_previous_cars(OUTPUT_FILE)

        merged_cars: list[dict[str, Any]] = []
        matched_count = 0
        manual_count = 0

        for api_car in api_cars:
            xml_match = find_xml_match(api_car, xml_cars)
            item_id = str(api_car["id"])

            if xml_match is not None:
                matched_count += 1
                print(
                    "XML найден:",
                    f"{api_car['brand']} {api_car['model']} {api_car['year']}",
                    f"— фото: {len(xml_match.images)}",
                )
            else:
                print(
                    "XML не найден:",
                    f"{api_car['brand']} {api_car['model']} {api_car['year']}",
                    f"{api_car['mileage']} км",
                )

            if item_id in manual_media:
                manual_count += 1
                print("Ручные данные применены:", item_id)

            merged_cars.append(
                merge_all_sources(
                    api_car,
                    xml_match,
                    previous_cars,
                    manual_media,
                )
            )

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
