#!/usr/bin/env python3
"""
Синхронизация активных объявлений Avito с cars-data.js сайта Юникар.

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
from typing import Any

import requests

BASE_URL = "https://api.avito.ru"
TOKEN_URL = f"{BASE_URL}/token"
ITEMS_URL = f"{BASE_URL}/core/v1/items"
ACCOUNT_URL = f"{BASE_URL}/core/v1/accounts/self"
OUTPUT_FILE = "cars-data.js"

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
        body = response.text[:1000]
        raise AvitoError(
            f"Avito API вернул HTTP {response.status_code} для {url}: {body}"
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
        "User-Agent": "UnicarWebsiteSync/1.0",
    }


def get_account(token: str) -> dict[str, Any]:
    return request_json("GET", ACCOUNT_URL, headers=bearer(token))


def extract_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Поддерживает несколько вариантов обёртки ответа Avito."""
    candidates = [
        payload.get("resources"),
        payload.get("items"),
        payload.get("result"),
        payload.get("data"),
    ]

    for value in candidates:
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            for key in ("items", "resources", "result"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return [x for x in nested if isinstance(x, dict)]

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
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_images(item: dict[str, Any]) -> list[str]:
    raw = first_value(
        item,
        "images",
        "photos",
        "image_urls",
        "media.images",
        default=[],
    )

    images: list[str] = []

    if isinstance(raw, str):
        images.append(raw)

    elif isinstance(raw, list):
        for image in raw:
            if isinstance(image, str):
                images.append(image)
            elif isinstance(image, dict):
                url = first_value(
                    image,
                    "url",
                    "uri",
                    "src",
                    "1280x960",
                    "640x480",
                    "original",
                )
                if url:
                    images.append(str(url))

    elif isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, str) and value.startswith("http"):
                images.append(value)
            elif isinstance(value, list):
                for entry in value:
                    if isinstance(entry, str):
                        images.append(entry)
                    elif isinstance(entry, dict):
                        url = first_value(entry, "url", "uri", "src", "original")
                        if url:
                            images.append(str(url))

    main_image = first_value(
        item,
        "image_url",
        "image",
        "main_image",
        "photo",
        "preview.url",
    )
    if isinstance(main_image, str) and main_image:
        images.insert(0, main_image)

    result: list[str] = []
    seen: set[str] = set()
    for url in images:
        url = str(url).strip()
        if url.startswith("http") and url not in seen:
            result.append(url)
            seen.add(url)

    return result


def extract_param(item: dict[str, Any], names: tuple[str, ...]) -> str:
    raw = first_value(item, "params", "parameters", "attributes", default=[])

    if isinstance(raw, dict):
        for name in names:
            for key, value in raw.items():
                if str(key).lower() == name.lower():
                    return str(value)

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            label = str(
                first_value(entry, "name", "title", "label", "slug", default="")
            ).lower()
            if any(name.lower() == label for name in names):
                return str(first_value(entry, "value", "value_title", "text", default=""))

    return ""


def parse_title(title: str) -> tuple[str, str, int]:
    title = re.sub(r"\s+", " ", title).strip()
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    year = int(year_match.group(1)) if year_match else 0

    without_year = re.sub(r"\b(19\d{2}|20\d{2})\b", "", title)
    without_year = re.sub(r"[,|•]+", " ", without_year)
    parts = without_year.split()

    brand = parts[0] if parts else ""
    model = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
    return brand, model, year


def normalize_transmission(value: str) -> str:
    s = value.lower().strip()
    if not s:
        return ""
    if any(x in s for x in ("механ", "мкпп")) or s == "mt":
        return "механика"
    if any(x in s for x in ("автомат", "акпп")) or s == "at":
        return "автомат"
    if any(x in s for x in ("робот", "dsg", "amt")):
        return "робот"
    if any(x in s for x in ("вариатор", "cvt")):
        return "вариатор"
    return value


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    item_id = to_int(first_value(item, "id", "item_id", "avito_id"))

    title = str(first_value(item, "title", "name", default="")).strip()
    parsed_brand, parsed_model, parsed_year = parse_title(title)

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
    )

    transmission_raw = str(
        first_value(item, "transmission", "params.transmission", default="")
        or extract_param(item, ("Коробка передач", "КПП", "transmission"))
    )

    price = to_int(first_value(item, "price", "price.value", "price_amount", default=0))
    description = clean_text(first_value(item, "description", "text", default=""))

    return {
        "id": item_id,
        "brand": brand,
        "model": model,
        "year": year,
        "price": price,
        "mileage": mileage,
        "transmission": normalize_transmission(transmission_raw),
        "description": description,
        "images": extract_images(item),
        "avitoUrl": str(first_value(item, "url", "item_url", "link", default="")),
    }


def is_car(car: dict[str, Any]) -> bool:
    # Не публикуем полностью пустые записи.
    return bool(car["id"] and (car["brand"] or car["model"]) and car["price"])


def write_cars_js(cars: list[dict[str, Any]]) -> None:
    content = (
        "// ========== АВТОМОБИЛИ ИЗ AVITO API ==========\n"
        "// Файл обновляется автоматически GitHub Actions. Не редактируйте вручную.\n\n"
        "const cars = "
        + json.dumps(cars, ensure_ascii=False, indent=4)
        + ";\n"
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)


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
        cars = [normalize_item(item) for item in raw_items]
        cars = [car for car in cars if is_car(car)]

        cars.sort(key=lambda car: (car["year"], car["price"]), reverse=True)
        write_cars_js(cars)

        print(f"Активных объявлений получено: {len(raw_items)}")
        print(f"Автомобилей записано в {OUTPUT_FILE}: {len(cars)}")

        if raw_items and not cars:
            raise AvitoError(
                "API вернул объявления, но ни одно не удалось преобразовать в автомобиль. "
                "Посмотрите структуру ответа в документации вашего приложения Avito API."
            )

        return 0

    except (AvitoError, requests.RequestException) as exc:
        print(f"Ошибка синхронизации: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
