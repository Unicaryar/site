import xml.etree.ElementTree as ET
import json
import re
import sys

# --- Конфигурация ---
XML_URL = "https://export.maxposter.ru/avito/12981.xml"
OUTPUT_FILE = "/home/c/cs608308/unicar-yar.ru/public_html/cars-data.js"
# -------------------

def clean_html_to_text(html_content):
    if not html_content:
        return ""
    text = html_content
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'<p[^>]*>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'#[а-яА-Яa-zA-Z0-9_]+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    return text

def extract_engine_and_transmission(modification):
    if not modification:
        return ""
    text = re.sub(r'\s*\([^)]*\)', '', modification)
    text = re.sub(r'\s+(MPI|Ti-VCT|GDI|TDI|FSI|CVVT|VVT-i)\s+', ' ', text)
    text = ' '.join(text.split())
    return text

def normalize_transmission(transmission_str):
    if not transmission_str:
        return ""
    s = transmission_str.lower().strip()
    if "механик" in s or s in ("mt", "мкпп"):
        return "механика"
    if "автомат" in s or s in ("at", "акпп"):
        return "автомат"
    if "робот" in s or s in ("amt", "дсг"):
        return "робот"
    if "вариатор" in s or s in ("cvt", "вариатор"):
        return "вариатор"
    return transmission_str

def format_model_name(model, modification):
    engine_trans = extract_engine_and_transmission(modification)
    if engine_trans:
        return f"{model} {engine_trans}"
    return model

def parse_xml_to_js():
    # Загрузка XML из интернета
    import urllib.request
    try:
        with urllib.request.urlopen(XML_URL) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
    except Exception as e:
        print(f"Ошибка загрузки XML: {e}")
        sys.exit(1)

    cars_data = []
    car_id = 1

    for ad in root.findall('Ad'):
        make = ad.findtext('Make', '')
        model = ad.findtext('Model', '')
        modification = ad.findtext('Modification', '')
        full_model_name = format_model_name(model, modification)
        year = ad.findtext('Year', '')
        price = ad.findtext('Price', '')
        mileage = ad.findtext('Kilometrage', '')
        description_raw = ad.findtext('Description', '')
        transmission_raw = ad.findtext('Transmission', '')
        transmission = normalize_transmission(transmission_raw)
        description_text = clean_html_to_text(description_raw)

        images = []
        images_elem = ad.find('Images')
        if images_elem is not None:
            for img in images_elem.findall('Image'):
                url = img.get('url')
                if url:
                    images.append(url)

        if not images:
            images = ["https://via.placeholder.com/300x200?text=No+Image"]

        car = {
            "id": car_id,
            "brand": make,
            "model": full_model_name,
            "year": int(year) if year else 0,
            "price": int(price) if price else 0,
            "mileage": int(mileage) if mileage else 0,
            "transmission": transmission,
            "description": description_text,
            "images": images
        }

        cars_data.append(car)
        car_id += 1

    # Генерация JS-файла
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("// ========== БАЗА ДАННЫХ АВТОМОБИЛЕЙ ==========\n")
        f.write("// Этот файл можно редактировать и заменять независимо от index.html\n\n")
        f.write("const cars = ")
        json_str = json.dumps(cars_data, ensure_ascii=False, indent=4)
        f.write(json_str)
        f.write(";\n")

    print(f"✅ Файл {OUTPUT_FILE} успешно создан.")
    print(f"📊 Обработано автомобилей: {len(cars_data)}")

if __name__ == "__main__":
    parse_xml_to_js()