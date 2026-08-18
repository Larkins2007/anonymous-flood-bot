# Pure group-role logic; no Telegram dependency.
import re

ROLE_CATALOG = [('Альбедо', 'Albedo', 'Мондштадт'), ('Барбара', 'Barbara', 'Мондштадт'), ('Беннет', 'Bennett', 'Мондштадт'), ('Варка', 'Varka', 'Мондштадт'), ('Венти', 'Venti', 'Мондштадт'), ('Далия', 'Dahlia', 'Мондштадт'), ('Джинн', 'Jean', 'Мондштадт'), ('Дилюк', 'Diluc', 'Мондштадт'), ('Диона', 'Diona', 'Мондштадт'), ('Дурин', 'Durin', 'Мондштадт'), ('Кли', 'Klee', 'Мондштадт'), ('Кэйя', 'Kaeya', 'Мондштадт'), ('Лиза', 'Lisa', 'Мондштадт'), ('Лоэн', 'Lohen', 'Мондштадт'), ('Мика', 'Mika', 'Мондштадт'), ('Мона', 'Mona', 'Мондштадт'), ('Ноэлль', 'Noelle', 'Мондштадт'), ('Прюн', 'Prune', 'Мондштадт'), ('Рейзор', 'Razor', 'Мондштадт'), ('Розария', 'Rosaria', 'Мондштадт'), ('Сахароза', 'Sucrose', 'Мондштадт'), ('Фишль', 'Fischl', 'Мондштадт'), ('Эмбер', 'Amber', 'Мондштадт'), ('Эола', 'Eula', 'Мондштадт'), ('Бай Чжу', 'Baizhu', 'Li Yue'), ('Бэй Доу', 'Beidou', 'Li Yue'), ('Гань Юй', 'Ganyu', 'Li Yue'), ('Е Лань', 'Yelan', 'Li Yue'), ('Ка Мин', 'Gaming', 'Li Yue'), ('Кэ Цин', 'Keqing', 'Li Yue'), ('Лань Янь', 'Lan Yan', 'Li Yue'), ('Нин Гуан', 'Ningguang', 'Li Yue'), ('Син Цю', 'Xingqiu', 'Li Yue'), ('Синь Янь', 'Xinyan', 'Li Yue'), ('Сян Лин', 'Xiangling', 'Li Yue'), ('Сянь Юнь', 'Xianyun', 'Li Yue'), ('Сяо', 'Xiao', 'Li Yue'), ('Ху Тао', 'Hu Tao', 'Li Yue'), ('Цзы Бай', 'Zibai', 'Li Yue'), ('Ци Ци', 'Qiqi', 'Li Yue'), ('Чжун Ли', 'Zhongli', 'Li Yue'), ('Чунь Юнь', 'Chongyun', 'Li Yue'), ('Шэнь Хэ', 'Shenhe', 'Li Yue'), ('Юнь Цзинь', 'Yun Jin', 'Li Yue'), ('Янь Фэй', 'Yanfei', 'Li Yue'), ('Яо Яо', 'Yaoyao', 'Li Yue'), ('Аратаки Итто', 'Arataki Itto', 'Inazuma'), ('Аяка', 'Ayaka', 'Inazuma'), ('Аято', 'Ayato', 'Inazuma'), ('Горо', 'Gorou', 'Inazuma'), ('Ёимия', 'Yoimiya', 'Inazuma'), ('Кадзуха', 'Kaedehara Kazuha', 'Inazuma'), ('Кирара', 'Kirara', 'Inazuma'), ('Кокоми', 'Kokomi', 'Inazuma'), ('Мидзуки', 'Yumemizuki Mizuki', 'Inazuma'), ('Райдэн Эи', 'Raiden Ei', 'Inazuma'), ('Сара', 'Kujou Sara', 'Inazuma'), ('Саю', 'Sayu', 'Inazuma'), ('Синобу', 'Kuki Shinobu', 'Inazuma'), ('Тома', 'Thoma', 'Inazuma'), ('Хэйдзо', 'Shikanoin Heizou', 'Inazuma'), ('Яэ Мико', 'Yae Miko', 'Inazuma'), ('Аль-Хайтам', 'Alhaitham', 'Sumeru'), ('Дори', 'Dori', 'Sumeru'), ('Дэхья', 'Dehya', 'Sumeru'), ('Кавех', 'Kaveh', 'Sumeru'), ('Кандакия', 'Candace', 'Sumeru'), ('Коллеи', 'Collei', 'Sumeru'), ('Лайла', 'Layla', 'Sumeru'), ('Нахида', 'Nahida', 'Sumeru'), ('Нилу', 'Nilou', 'Sumeru'), ('Сайно', 'Cyno', 'Sumeru'), ('Сетос', 'Sethos', 'Sumeru'), ('Странник', 'Wanderer', 'Sumeru'), ('Тигнари', 'Tighnari', 'Sumeru'), ('Фарузан', 'Faruzan', 'Sumeru'), ('Клоринда', 'Clorinde', 'Fontaine'), ('Лини', 'Lyney', 'Fontaine'), ('Линетт', 'Lynette', 'Fontaine'), ('Навия', 'Navia', 'Fontaine'), ('Нёвиллет', 'Neuvillette', 'Fontaine'), ('Ризли', 'Wriothesley', 'Fontaine'), ('Сиджвин', 'Sigewinne', 'Fontaine'), ('Тиори', 'Chiori', 'Fontaine'), ('Фремине', 'Freminet', 'Fontaine'), ('Фурина', 'Furina', 'Fontaine'), ('Шарлотта', 'Charlotte', 'Fontaine'), ('Эмилия', 'Emilie', 'Fontaine'), ('Эскофье', 'Escoffier', 'Fontaine'), ('Вареса', 'Varesa', 'Natlan'), ('Иансан', 'Iansan', 'Natlan'), ('Ифа', 'Ifa', 'Natlan'), ('Качина', 'Kachina', 'Natlan'), ('Кинич', 'Kinich', 'Natlan'), ('Мавуика', 'Mavuika', 'Natlan'), ('Муалани', 'Mualani', 'Natlan'), ('Оророн', 'Ororon', 'Natlan'), ('Ситлали', 'Citlali', 'Natlan'), ('Часка', 'Chasca', 'Natlan'), ('Шилонен', 'Xilonen', 'Natlan'), ('Айно', 'Aino', 'Nod-Krai'), ('Иллуги', 'Illuga', 'Nod-Krai'), ('Инеффа', 'Ineffa', 'Nod-Krai'), ('Коломбина', 'Columbina', 'Nod-Krai'), ('Лаума', 'Lauma', 'Nod-Krai'), ('Линнея', 'Linnea', 'Nod-Krai'), ('Нефер', 'Nefer', 'Nod-Krai'), ('Флинс', 'Flins', 'Nod-Krai'), ('Ягода', 'Jahoda', 'Nod-Krai'), ('Алёша', 'Alyosha', 'Snezhnaya'), ('Арлекино', 'Arlecchino', 'Snezhnaya'), ('Валера', 'Valeriy', 'Snezhnaya'), ('Весна', 'Vesna', 'Snezhnaya'), ('Водяница', 'Vodyanitsa', 'Snezhnaya'), ('Даника', 'Danica', 'Snezhnaya'), ('Дотторе', 'Dottore', 'Snezhnaya'), ('Капитано', 'Capitano', 'Snezhnaya'), ('Митя', 'Mitya', 'Snezhnaya'), ('Ной', 'Noy', 'Snezhnaya'), ('Одетта', 'Odette', 'Snezhnaya'), ('Панталоне', 'Pantalone', 'Snezhnaya'), ('Пьеро', 'Pierro', 'Snezhnaya'), ('Пульчинелла', 'Pulcinella', 'Snezhnaya'), ('Сандроне', 'Sandrone', 'Snezhnaya'), ('Синьора', 'Signora', 'Snezhnaya'), ('Тарталья', 'Tartaglia', 'Snezhnaya'), ('Царица', 'Tsaritsa', 'Snezhnaya'), ('Ведрфельнир', 'Vedrfolnir', "Khaenri'ah"), ('Дайнслейф', 'Dainsleif', "Khaenri'ah"), ('Рери', 'Rerir', "Khaenri'ah"), ('Сурталоги', 'Surtalogi', "Khaenri'ah"), ('Толиндис', 'Tolindis', "Khaenri'ah"), ('Хальфдан', 'Halfdan', "Khaenri'ah"), ('Хрофтатюр', 'Hroptatyr', "Khaenri'ah"), ('Алиса', 'Alice', 'Shabash'), ('Андерсдоттер', 'Andersdotter', 'Shabash'), ('Барбелот', 'Barbelot', 'Shabash'), ('Николь Рейн', 'Nicole Reeyn', 'Shabash'), ('Октавия', 'Octavia', 'Shabash'), ('Рэйндоттир', 'Rhinedottir', 'Shabash'), ('Астарот', 'Istaroth', 'Shadows'), ('Асмодей', 'Asmoday', 'Shadows'), ('Набериус', 'Naberius', 'Shadows'), ('Ронова', 'Ronova', 'Shadows'), ('Итэр', 'Aether', 'Another'), ('Люмин', 'Lumine', 'Another'), ('Паймон', 'Paimon', 'Another'), ('Скирк', 'Skirk', 'Another')]

ROLE_BY_KEY = {}
for _ru, _en, _region in ROLE_CATALOG:
    _k = re.sub(r"\s+", " ", _ru.strip()).casefold()
    ROLE_BY_KEY[_k] = {"name": _ru, "english": _en, "region": _region}

MATH_ITALIC = {'A': '𝑨', 'B': '𝑩', 'C': '𝑪', 'D': '𝑫', 'E': '𝑬', 'F': '𝑭', 'G': '𝑮', 'H': '𝑯', 'I': '𝑰', 'J': '𝑱', 'K': '𝑲', 'L': '𝑳', 'M': '𝑴', 'N': '𝑵', 'O': '𝑶', 'P': '𝑷', 'Q': '𝑸', 'R': '𝑹', 'S': '𝑺', 'T': '𝑻', 'U': '𝑼', 'V': '𝑽', 'W': '𝑾', 'X': '𝑿', 'Y': '𝒀', 'Z': '𝒁', 'a': '𝒂', 'b': '𝒃', 'c': '𝒄', 'd': '𝒅', 'e': '𝒆', 'f': '𝒇', 'g': '𝒈', 'h': '𝒉', 'i': '𝒊', 'j': '𝒋', 'k': '𝒌', 'l': '𝒍', 'm': '𝒎', 'n': '𝒏', 'o': '𝒐', 'p': '𝒑', 'q': '𝒒', 'r': '𝒓', 's': '𝒔', 't': '𝒕', 'u': '𝒖', 'v': '𝒗', 'w': '𝒘', 'x': '𝒙', 'y': '𝒚', 'z': '𝒛'}

def normalize_role(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()

def role_for(value):
    return ROLE_BY_KEY.get(normalize_role(value))

def make_tag(english_name):
    short = english_name
    # Keep the visible tag short enough for Telegram's 16-character limit.
    aliases = {
        "Kaedehara Kazuha": "Kazuha",
        "Yumemizuki Mizuki": "Mizuki",
        "Kujou Sara": "Sara",
        "Kuki Shinobu": "Shinobu",
        "Shikanoin Heizou": "Heizou",
        "Raiden Ei": "Ei",
    }
    short = aliases.get(short, short)
    decorated = "❦" + "".join(MATH_ITALIC.get(ch, ch) for ch in short) + "❦"
    if len(decorated) <= 16:
        return decorated
    safe = "".join(MATH_ITALIC.get(ch, ch) for ch in short)
    return safe[:16]

def parse_kall(text):
    m = re.match(r"^\s*калл\b\s+(.+?)\s*$", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()

def utf16_slice(text, offset, length):
    raw = text.encode("utf-16-le")
    start = offset * 2
    end = start + length * 2
    return raw[start:end].decode("utf-16-le", errors="ignore")
