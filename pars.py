import xml.etree.ElementTree as ET
import json
import re

def clean_html_to_text(html_content):
    """Преобразует HTML в простой текст с переносами строк (как в примере cars-data.js)"""
    if not html_content:
        return ""
    
    text = html_content
    
    # Заменяем <br> и <p> на переносы строк
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'<p[^>]*>', '', text)
    
    # Удаляем остальные HTML-теги
    text = re.sub(r'<[^>]+>', '', text)
    
    # Удаляем хэштеги (#автоспробегом и т.д.)
    text = re.sub(r'#[а-яА-Яa-zA-Z0-9_]+', '', text)
    
    # Убираем множественные переносы строк
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Убираем пустые строки в начале и конце
    text = text.strip()
    
    # Экранируем кавычки для JSON
    text = text.replace('"', '\\"')
    
    return text

def extract_engine_and_transmission(modification):
    """
    Извлекает объем двигателя и тип КПП из строки modification
    Примеры:
    "1.4 MT (98 л.с.)" -> "1.4 MT"
    "1.6 AMT (125 л.с.)" -> "1.6 AMT"
    "1.1 AT (64 л.с.)" -> "1.1 AT"
    "1.6 MPI MT (102 л.с.)" -> "1.6 MT"
    """
    if not modification:
        return ""
    
    # Убираем лошадиные силы в скобках
    text = re.sub(r'\s*\([^)]*\)', '', modification)
    
    # Убираем лишние слова типа MPI, Ti-VCT и т.д.
    text = re.sub(r'\s+(MPI|Ti-VCT|GDI|TDI|FSI|CVVT|VVT-i)\s+', ' ', text)
    
    # Приводим к стандартному виду (убираем лишние пробелы)
    text = ' '.join(text.split())
    
    return text

def normalize_transmission(transmission_str):
    """Приводит тип КПП к стандартному виду: механика, автомат, робот, вариатор"""
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
    return transmission_str  # fallback

def format_model_name(model, modification):
    """
    Форматирует название модели в формате: "Focus 1.6 AMT" или "Picanto 1.1 AT"
    """
    engine_trans = extract_engine_and_transmission(modification)
    
    if engine_trans:
        return f"{model} {engine_trans}"
    return model

def parse_xml_to_js(xml_file_path, output_js_path):
    tree = ET.parse(xml_file_path)
    root = tree.getroot()

    cars_data = []
    car_id = 1

    for ad in root.findall('Ad'):
        make = ad.findtext('Make', '')
        model = ad.findtext('Model', '')
        modification = ad.findtext('Modification', '')
        
        # Формируем модель с объемом и КПП
        full_model_name = format_model_name(model, modification)
        
        year = ad.findtext('Year', '')
        price = ad.findtext('Price', '')
        mileage = ad.findtext('Kilometrage', '')
        description_raw = ad.findtext('Description', '')
        
        # Читаем тип трансмиссии и нормализуем
        transmission_raw = ad.findtext('Transmission', '')
        transmission = normalize_transmission(transmission_raw)
        
        # Преобразуем HTML в обычный текст с переносами
        description_text = clean_html_to_text(description_raw)
        
        # Собираем ВСЕ изображения (без ограничений)
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
            "transmission": transmission,      # новое поле
            "description": description_text,
            "images": images
        }
        
        cars_data.append(car)
        car_id += 1

    # Генерируем JS-файл
    with open(output_js_path, 'w', encoding='utf-8') as f:
        f.write("// ========== БАЗА ДАННЫХ АВТОМОБИЛЕЙ ==========\n")
        f.write("// Этот файл можно редактировать и заменять независимо от index.html\n\n")
        f.write("const cars = ")
        
        json_str = json.dumps(cars_data, ensure_ascii=False, indent=4)
        f.write(json_str)
        f.write(";\n")

    print(f"✅ Файл {output_js_path} успешно создан.")
    print(f"📊 Обработано автомобилей: {len(cars_data)}")
    
    total_photos = sum(len(car['images']) for car in cars_data)
    print(f"📸 Всего фотографий: {total_photos}")
    
    # Показываем примеры первых автомобилей
    if cars_data:
        print(f"\n📌 Примеры сформированных данных:")
        for i, car in enumerate(cars_data[:5]):
            print(f"   {i+1}. {car['brand']} {car['model']} — КПП: {car['transmission']}")

if __name__ == "__main__":
    parse_xml_to_js("12981.xml", "cars-data.js")