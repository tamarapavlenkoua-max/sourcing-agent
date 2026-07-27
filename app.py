"""
=====================================================================
AI SOURCING AGENT - SKELAR (MVP)
=====================================================================
Що це таке (для нетехнічної людини):
Це веб-додаток на Streamlit, який автоматично шукає кандидатів на
LinkedIn через Apify, перевіряє їх штучним інтелектом (Claude) за
твоїми жорсткими критеріями (локація, стоп-лист компаній, релевантний
досвід) і крутиться по колу ("agentic loop"), поки не назбирає рівно
стільки ідеальних (A-tier) кандидатів, скільки ти вказала - або поки
не вичерпає ліміт спроб (safety limit), щоб не зациклитись назавжди.

ЯК ЗАПУСТИТИ ЛОКАЛЬНО (для перевірки перед деплоєм):
1. Встанови Python 3.10+ (якщо ще нема): https://www.python.org/downloads/
2. Відкрий термінал у папці з цим файлом і виконай:
   pip install -r requirements.txt
3. Запусти додаток:
   streamlit run app.py
4. У браузері відкриється сторінка. Введи свої API-ключі в лівій панелі.

ЯК ЗАДЕПЛОЇТИ НА STREAMLIT COMMUNITY CLOUD (безкоштовний хостинг):
1. Заведи GitHub-репозиторій і заливай туди app.py + requirements.txt.
2. Зайди на https://share.streamlit.io , натисни "New app".
3. Вибери свій репозиторій, файл app.py, гілку main.
4. Натисни Deploy. Ключі API вводяться прямо у формі додатку
   (нікуди більше вставляти не треба).

ЩО ПОКИ НЕ ПІДКЛЮЧЕНО (навмисно, за домовленістю):
Ashby-перевірка (Agent 2) зараз працює як "заглушка" (stub): якщо
Ashby API key не введений - кандидат просто позначається як
"Не перевірено в ATS" і йде далі по циклу. Коли отримаєш Ashby Admin
API key - шукай коментар "TODO ASHBY" нижче по коду, там точно
вказано, що і як дописати.
=====================================================================
"""

import streamlit as st
import json
import re
import time
import io
import csv
import math

import anthropic
from apify_client import ApifyClient

# ---------------------------------------------------------------------------
# КОНСТАНТИ, ЯКІ МОЖНА МІНЯТИ
# ---------------------------------------------------------------------------

# У ТЗ просили "Claude 3.5 Sonnet" - але це вже стара версія моделі.
# Тут стоїть найновіший Sonnet. Якщо Anthropic випустить нову модель,
# просто заміни рядок нижче - решта коду міняти не треба.
CLAUDE_MODEL = "claude-sonnet-5"

# Актор Apify, який шукає профілі LinkedIn (той, що вказаний у ТЗ)
APIFY_ACTOR = "harvestapi/linkedin-profile-search"

# Офіційний актор Apify для Google-пошуку (SERP) - для X-ray каналу
GOOGLE_SEARCH_ACTOR = "apify/google-search-scraper"

# Актор, який дотягує повні дані LinkedIn-профілю за URL (для профілів,
# знайдених через Google X-ray, а не через прямий LinkedIn-пошук)
LINKEDIN_PROFILE_BY_URL_ACTOR = "harvestapi/linkedin-profile-scraper"

# Скільки сторінок Google-видачі сканувати за одну ітерацію (1 сторінка ≈ 10 посилань).
# Свідомо невелике число для MVP - більше коштуватиме дорожче й довше.
GOOGLE_XRAY_MAX_PAGES = 1

# Скільки разів Strategist може сам "полагодити" і повторити запит до Apify
# ВСЕРЕДИНІ однієї ітерації, якщо перша спроба дала помилку або 0 профілів
# (замість того щоб сліпо чекати наступної ітерації з тим самим браком).
MAX_APIFY_ATTEMPTS_PER_ITERATION = 3

# Локації, які завжди виключаємо з пошуку (додатковий захист "ніколи не Росія"
# ще на рівні самого пошуку в Apify, до того як профіль взагалі побачить AI)
HARD_EXCLUDE_LOCATIONS = ["Russia"]

# ВАЖЛИВО: LinkedIn (а через нього і Apify-актор) розуміє в полі "locations"
# ТІЛЬКИ реальні географічні назви (країна/місто), які є в автопідказках
# LinkedIn-пошуку. Значення на кшталт "Remote" не розпізнаються і актор
# падає з 404 на ВСЮ сторінку результатів (перевірено на реальному запуску:
# "Input location 'Remote' is not recognized by Linkedin"). Тому такі
# значення відсіюємо ще до відправки в Apify, незалежно від того, що
# згенерував Strategist.
INVALID_LOCATION_TOKENS = {
    "remote", "remote-friendly", "remote friendly", "anywhere", "worldwide",
    "global", "hybrid", "будь-де", "віддалено", "дистанційно", "не має значення",
}


def clean_locations(locations: list) -> list:
    """Прибирає значення, які LinkedIn не розпізнає як реальну локацію
    (напр. 'Remote') - інакше весь запуск актора падає на 404."""
    return [
        loc for loc in (locations or [])
        if loc and loc.strip().lower() not in INVALID_LOCATION_TOKENS
    ]

# Скільки мілісекунд чекати між LLM-викликами, щоб не впертися в rate limit
LLM_CALL_DELAY_SEC = 0.4


# ---------------------------------------------------------------------------
# ДОПОМІЖНІ ФУНКЦІЇ: ВИТЯГТИ ДАНІ З ПРОФІЛЮ (структура відповіді Apify-актора
# може відрізнятись, тому скрізь є запасні варіанти назв полів)
# ---------------------------------------------------------------------------

def get_profile_name(p: dict) -> str:
    if p.get("fullName"):
        return p["fullName"]
    if p.get("name"):
        return p["name"]
    first = p.get("firstName", "")
    last = p.get("lastName", "")
    if first or last:
        return f"{first} {last}".strip()
    return "Ім'я не знайдено"


def get_profile_url(p: dict) -> str:
    for key in ("linkedinUrl", "profileUrl", "url"):
        if p.get(key):
            return p[key]
    if p.get("publicIdentifier"):
        return f"https://www.linkedin.com/in/{p['publicIdentifier']}"
    return ""


def get_profile_headline(p: dict) -> str:
    if p.get("headline"):
        return p["headline"]
    cp = p.get("currentPosition")
    if isinstance(cp, dict):
        return cp.get("title", "")
    if isinstance(cp, list) and cp:
        return cp[0].get("title", "")
    return "н/д"


def profile_to_compact_text(p: dict, max_chars: int = 6000) -> str:
    """Перетворює сирий JSON профілю в текст для передачі в Claude.
    Обрізаємо, щоб не роздувати вартість виклику на дуже великих профілях."""
    try:
        text = json.dumps(p, ensure_ascii=False)
    except Exception:
        text = str(p)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# ДОПОМІЖНА ФУНКЦІЯ: ВИКЛИК CLAUDE З ОЧІКУВАННЯМ JSON-ВІДПОВІДІ
# ---------------------------------------------------------------------------

def ask_claude_json(client: anthropic.Anthropic, system_prompt: str, user_prompt: str) -> dict:
    """Викликає Claude і намагається розпарсити JSON з відповіді.
    Якщо щось пішло не так - повертає {"error": "..."} замість падіння всього циклу."""
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        json_str = match.group(0) if match else raw_text
        return json.loads(json_str)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        time.sleep(LLM_CALL_DELAY_SEC)


# ---------------------------------------------------------------------------
# AGENT 1: STRATEGIST - будує / перебудовує параметри пошуку для Apify
# ---------------------------------------------------------------------------

def agent_strategist(client, job_description: str, search_history: list, still_needed: int, recruiter_note: str = "") -> dict:
    system_prompt = """Ти - Strategist, агент з побудови пошукових запитів для LinkedIn
через Apify-актор harvestapi/linkedin-profile-search.

Твоє завдання: на основі опису вакансії згенерувати параметри пошуку.
Якщо в history вже є попередні спроби - подивись, скільки вони дали
релевантних кандидатів, і зроби НОВИЙ, ІНШИЙ запит (інші ключові слова,
ширші/вужчі тайтли, інші локації), щоб не повторювати той самий пошук.

Якщо рекрутер дав текстову корективу для цієї ітерації (recruiter_note) -
це НАЙВИЩИЙ пріоритет: врахуй її першою, навіть якщо це суперечить твоїй
попередній стратегії. Наприклад, якщо рекрутер написав "шукай ще в Львові" -
додай Львів у locations. Якщо написав "тайтл X не підходить" - прибери
цей тайтл з переліку і не повертайся до нього.

КРИТИЧНО ВАЖЛИВО про searchQuery vs currentJobTitles (перевірено на
реальних запусках - неправильна комбінація дає 0 результатів, хоча кожен
фільтр окремо на сайті Apify працює):
Apify-актор застосовує searchQuery і currentJobTitles як AND, а не OR.
Якщо заповнити ОБИДВА одночасно (напр. searchQuery="Data Analyst" І
currentJobTitles=["Data Analyst","Product Analyst",...]) - профіль повинен
пройти ОБИДВА фільтри одночасно, і в комбінації з locations це майже
завжди дає 0, навіть якщо кандидати реально є.
Тому роби так: усі альтернативні тайтли об'єднай ОДНИМ boolean-рядком
через OR прямо в searchQuery, наприклад:
"Product Analyst" OR "Data Analyst" OR "Growth Product Analyst"
А currentJobTitles залишай ПОРОЖНІМ масивом [] за замовчуванням - не
дублюй туди ті самі тайтли. Заповнюй currentJobTitles окремо ТІЛЬКИ якщо
рекрутер явно попросив жорстко відфільтрувати по точному тайтлу (це
окремий, свідомо вужчий режим, не комбінуй з широким searchQuery в
той самий момент).

КРИТИЧНО ВАЖЛИВО про locations: пиши ТІЛЬКИ реальні географічні назви -
країни або міста (наприклад "Ukraine", "Kyiv", "Lviv", "Poland", "Warsaw").
НІКОЛИ не пиши туди "Remote", "Anywhere", "Worldwide", "Hybrid" чи будь-що
не-географічне - LinkedIn не розпізнає такі значення, і через це actor
падає з помилкою на ВЕСЬ запит (перевірено на практиці), а не просто
ігнорує це значення. Якщо вакансія дозволяє віддалену роботу - це вже
означає ширший список країн/міст у locations, а не додавання слова "Remote".

Відповідай ТІЛЬКИ у форматі JSON, без пояснень навколо:
{
  "searchQuery": "усі альтернативні тайтли ОДНИМ рядком через OR, напр. '\\"Growth Marketing Manager\\" OR \\"Performance Marketing Manager\\"'",
  "currentJobTitles": [],
  "locations": ["Ukraine", "Kyiv", "Lviv", "..."],
  "reasoning": "1-2 речення чому саме такий запит цього разу, і як врахована корректива рекрутера (якщо була)"
}
"""
    user_prompt = f"""Опис вакансії:
{job_description}

Скільки A-tier кандидатів ще потрібно знайти: {still_needed}

Текстова корректива від рекрутера саме для цієї ітерації (може бути порожньою):
{recruiter_note or "(немає)"}

Історія попередніх ітерацій цього пошуку (може бути порожньою):
{json.dumps(search_history, ensure_ascii=False, indent=2)}
"""
    result = ask_claude_json(client, system_prompt, user_prompt)
    if "error" in result:
        # запасний варіант, щоб цикл не падав, якщо LLM видав щось незрозуміле
        result = {"searchQuery": job_description[:100], "currentJobTitles": [], "locations": ["Ukraine"], "reasoning": "fallback"}
    return result


# ---------------------------------------------------------------------------
# АГЕНТ "PM" - ДІАГНОСТИКА І ПОЛАГОДЖЕННЯ ЗАПИТУ ВСЕРЕДИНІ ІТЕРАЦІЇ
# ---------------------------------------------------------------------------
# Apify - це інструмент, а не гарантія. Якщо перша спроба дала помилку або
# 0 профілів, ми не чекаємо сліпо наступної повної ітерації (і твоєї дії на
# чекпоінті) - Claude одразу як технічний PM аналізує, що могло піти не так,
# і сам лагодить запит, до MAX_APIFY_ATTEMPTS_PER_ITERATION спроб.

def agent_diagnose_and_repair(client, job_description: str, search_params: dict, apify_error, profiles_found: int, previous_attempts: list) -> dict | None:
    system_prompt = """Ти - технічний PM, який відповідає за якість запитів до Apify-актора
harvestapi/linkedin-profile-search. Твоя робота - не сліпо приймати
результат Apify, а як досвідчений PM одразу аналізувати, чому запит дав
помилку або підозріло мало/нуль профілів, і лагодити запит, не чекаючи
наступної повної ітерації циклу.

Відомі, перевірені на практиці причини поганого результату:
1. Локація, яку LinkedIn не розпізнає (напр. "Remote", "Anywhere", "Hybrid")
   - актор падає з 404 на ВЕСЬ запит, не просто ігнорує значення.
2. Одночасне заповнення і searchQuery, і currentJobTitles - Apify застосовує
   їх як AND, і разом з locations це часто дає 0, хоча кожен фільтр окремо
   на сайті Apify працює нормально. Правильно: усі альтернативні тайтли
   ОДНИМ boolean-рядком через OR у searchQuery, currentJobTitles - порожній.
3. Занадто вузький/специфічний searchQuery (забагато AND, занадто конкретні
   фрази) - спробуй ширший OR-варіант.
4. Технічна помилка виклику (мережа, формат) - спробуй спростити запит.

Тобі дають: параметри запиту, скільки профілів реально повернулось (0 -
підозріло, навіть без явної помилки), текст помилки (якщо була), і історію
попередніх невдалих спроб ЦІЄЇ Ж ітерації (щоб не повторити ту саму
помилку двічі).

Постав короткий діагноз і запропонуй ВИПРАВЛЕНИЙ запит.

Відповідай ТІЛЬКИ у форматі JSON:
{
  "diagnosis": "1-2 речення - в чому, на твою думку, була проблема",
  "searchQuery": "виправлений пошуковий запит (тайтли через OR, якщо їх кілька)",
  "currentJobTitles": [],
  "locations": ["виправлені локації, тільки реальні географічні назви"]
}
"""
    user_prompt = f"""Опис вакансії:
{job_description}

Параметри запиту, що використали цього разу:
{json.dumps(search_params, ensure_ascii=False)}

Скільки профілів реально повернулось: {profiles_found}
Текст помилки (якщо була): {apify_error or "(без помилки, просто підозріло мало/нуль профілів)"}

Попередні невдалі спроби САМЕ цієї ітерації (може бути порожньо):
{json.dumps(previous_attempts, ensure_ascii=False, indent=2)}
"""
    result = ask_claude_json(client, system_prompt, user_prompt)
    if "error" in result:
        return None
    return result


# ---------------------------------------------------------------------------
# APIFY: ЗАПУСК ПОШУКУ ПРОФІЛІВ
# ---------------------------------------------------------------------------

def extract_dataset_id(run) -> str | None:
    """apify-client в різних версіях/середовищах повертає результат запуску
    актора то як звичайний dict, то як типізований обʼєкт (звідси помилка
    "'Run' object is not subscriptable", коли код жорстко очікував dict).
    Підтримуємо обидва варіанти, замість того щоб покладатись на один формат."""
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    # Обʼєктний варіант (напр. pydantic-модель чи dataclass) - пробуємо
    # типові назви атрибутів
    for attr in ("defaultDatasetId", "default_dataset_id"):
        value = getattr(run, attr, None)
        if value:
            return value
    return None


def run_apify_search(apify_token: str, search_params: dict, max_items: int, start_page: int = 1) -> list:
    client = ApifyClient(apify_token)
    run_input = {
        "profileScraperMode": "Full",
        "searchQuery": search_params.get("searchQuery", ""),
        "maxItems": max_items,
        "startPage": start_page,
        "excludeLocations": HARD_EXCLUDE_LOCATIONS,
    }
    original_locations = search_params.get("locations") or []
    safe_locations = clean_locations(original_locations)
    dropped = [loc for loc in original_locations if loc not in safe_locations]
    if dropped:
        st.warning(f"Прибрала нерозпізнані LinkedIn-локації, щоб актор не впав: {dropped}")
    if safe_locations:
        run_input["locations"] = safe_locations
    # НЕ дублюємо currentJobTitles разом з searchQuery - Apify застосовує їх
    # як AND, і в комбінації з locations це часто дає 0 результатів навіть
    # коли кандидати реально є (перевірено на практиці). Тайтли вже мають
    # бути об'єднані OR-ом всередині searchQuery Strategist-ом.
    if search_params.get("currentJobTitles"):
        run_input["currentJobTitles"] = search_params["currentJobTitles"]

    try:
        run = client.actor(APIFY_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId у відповіді Apify (тип відповіді: {type(run).__name__})"
        items = list(client.dataset(dataset_id).iterate_items())
        return items, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# GOOGLE X-RAY: ДОДАТКОВИЙ КАНАЛ ПОШУКУ (той самий Apify-токен, без нового ключа)
# ---------------------------------------------------------------------------
# Ідея: пряма LinkedIn-видача (актор вище) обмежена тим, що бачить сам LinkedIn
# в своєму пошуку. Google-пошук з фільтром site:linkedin.com/in іноді знаходить
# публічні профілі, яких немає у видачі LinkedIn-пошуку. Це працює як другий,
# незалежний канал у тій самій ітерації - знайдені URL йдуть у той самий
# Evaluator/Critic, що і профілі з прямого пошуку.

def build_xray_query(search_params: dict) -> str:
    """Формує простий Google X-ray запит з тих самих параметрів, що згенерував
    Strategist. Без окремого виклику Claude - це просто текстовий шаблон,
    щоб не витрачати зайві LLM-виклики на кожну ітерацію."""
    titles = (search_params.get("currentJobTitles") or [])[:3]
    if not titles and search_params.get("searchQuery"):
        titles = [search_params["searchQuery"]]
    locations = (clean_locations(search_params.get("locations")) or ["Ukraine"])[:3]

    title_clause = " OR ".join(f'"{t}"' for t in titles if t)
    location_clause = " OR ".join(f'"{l}"' for l in locations if l)

    query = "site:linkedin.com/in"
    if title_clause:
        query += f" ({title_clause})"
    if location_clause:
        query += f" ({location_clause})"
    return query


def run_google_xray_search(apify_token: str, query: str) -> list:
    """Шукає LinkedIn-профілі через звичайний Google-пошук (актор
    apify/google-search-scraper), тим самим Apify-токеном - окремий Google
    API key не потрібен. Повертає список унікальних, очищених LinkedIn URL
    (Google дає лише заголовок/сніпет, не повний профіль - тому далі ще
    треба окремо дотягнути дані через fetch_linkedin_profiles_by_url)."""
    client = ApifyClient(apify_token)
    run_input = {"queries": query, "maxPagesPerQuery": GOOGLE_XRAY_MAX_PAGES}
    try:
        run = client.actor(GOOGLE_SEARCH_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId у відповіді Google-пошуку (тип: {type(run).__name__})"
        items = list(client.dataset(dataset_id).iterate_items())
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    urls = []
    for item in items:
        for r in (item.get("organicResults") or []):
            url = r.get("url", "")
            if "linkedin.com/in/" in url:
                clean_url = url.split("?")[0].rstrip("/")
                if clean_url not in urls:
                    urls.append(clean_url)
    return urls, None


def fetch_linkedin_profiles_by_url(apify_token: str, urls: list) -> list:
    """Дотягує повні дані профілю для URL, знайдених через Google X-ray
    (сам Google дає тільки заголовок і сніпет, не структуровані дані)."""
    if not urls:
        return [], None
    client = ApifyClient(apify_token)
    run_input = {"urls": urls}
    try:
        run = client.actor(LINKEDIN_PROFILE_BY_URL_ACTOR).call(run_input=run_input)
        dataset_id = extract_dataset_id(run)
        if not dataset_id:
            return [], f"Не знайшов defaultDatasetId (тип: {type(run).__name__})"
        return list(client.dataset(dataset_id).iterate_items()), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# AGENT 2: ATS CHECKER (Ashby) - ЗАГЛУШКА
# ---------------------------------------------------------------------------

def agent_ats_checker(ashby_api_key: str, profile: dict) -> dict:
    """
    Правила (за ТЗ):
    - Кандидата немає в Ashby -> proceed
    - Кандидат є, але НЕ на активній вакансії поточного рекрутера -> proceed,
      позначити 'Exists in ATS, free'
    - Кандидат є і активний на вакансії ІНШОГО рекрутера -> SKIP

    !!! TODO ASHBY !!!
    Зараз ця функція нічого реально не перевіряє, бо Ashby Admin API key
    ще не підключений. Коли ключ буде готовий, тут треба:
    1. Викликати Ashby REST API (https://developers.ashbyhq.com/) напряму
       (POST на https://api.ashbyhq.com/candidate.search з іменем/LinkedIn URL).
    2. Якщо candidate знайдений - викликати application.list по candidateId,
       дістати job.hiringTeam і порівняти recruiter з поточним юзером.
    3. Повернути один із статусів нижче замість "not_checked".

    Ashby працює через звичайний REST API з Admin API key - НЕ через MCP,
    бо MCP живе всередині Claude/Cowork-чату, а не всередині окремого
    Streamlit-додатку, який ти відкриваєш сама в браузері.
    """
    if not ashby_api_key:
        return {"status": "not_checked", "note": "Ashby ще не підключено - перевір вручну перед аутричем"}

    # Поки ключ є, але логіку ще не реалізовано - чесно позначаємо це,
    # а не вдаємо, що перевірка відбулась.
    return {"status": "not_implemented", "note": "Ashby key є, але інтеграцію ще не дописано (див. TODO ASHBY)"}


# ---------------------------------------------------------------------------
# AGENT 3: STRICT EVALUATOR
# ---------------------------------------------------------------------------

def agent_evaluator(client, job_description: str, profile_text: str, stop_list: list, allow_belarusians: bool) -> dict:
    system_prompt = f"""Ти - Strict Evaluator, найсуворіший рекрутер-оцінювач кандидатів у SKELAR.

Профіль кандидата - це реальний JSON з актора harvestapi/linkedin-profile-search.
Ось ТОЧНА структура полів, якими треба користуватись (перевірено на реальних
даних актора, не вгадуй інші назви):
- `currentPosition` (масив) - ПОТОЧНЕ місце роботи кандидата ЗАРАЗ. Дивись
  `currentPosition[].companyName`.
- `experience` (масив) - ВСЯ історія роботи, включно з поточною. Запис
  вважається поточним, якщо `endDate.text == "Present"`, інакше - це минуле
  місце роботи.
- `location.parsed.country` / `location.parsed.city` / `location.linkedinText` -
  структурована локація кандидата. Це основне джерело для визначення країни,
  а не здогадки з тексту "about" чи skills.

ЖОРСТКІ HARD-STOP ПРАВИЛА (якщо хоч одне спрацювало - hard_stop = true,
незалежно від будь-яких інших переваг кандидата):
1. За `location.parsed.country` / `location.linkedinText`: кандидат з Росії,
   живе в Росії, або явно вказує російське громадянство без ознак
   релокації/протидії режиму - HARD STOP.
   Білоруси дозволені ТІЛЬКИ якщо allow_belarusians = {allow_belarusians} і
   вони явно перебувають ЗА межами Білорусі.
2. Компанія САМЕ з `currentPosition[].companyName` (тобто ПОТОЧНИЙ
   роботодавець) збігається (навіть частково, без урахування регістру) з
   будь-якою компанією зі стоп-листа: {stop_list} -> HARD STOP.
   ВАЖЛИВО: якщо стоп-компанія зустрічається лише в `experience` як МИНУЛЕ
   місце роботи (endDate не "Present") - це НЕ hard stop. Колишні
   працівники стоп-компаній - це нормальна і навіть цільова аудиторія для
   найму, блокуємо тільки тих, хто працює там ЗАРАЗ.

Якщо hard_stop не спрацював - оціни:
- total_yoe: загальний досвід роботи в роках, порахований з усіх записів
  `experience` (число)
- relevant_yoe: досвід САМЕ в релевантній для вакансії сфері/ролі, теж з
  `experience` (число). Якщо загальний досвід 5 років, але релевантний
  тільки 1 рік - грейд визначається по relevant_yoe (тобто це Junior, а не
  Middle/Senior).
- grade: Junior / Middle / Senior / Lead (на основі relevant_yoe і рівня задач)
- tier: "A" (ідеальний збіг з вакансією), "B" (непоганий, але не ідеальний),
  "C" (слабкий збіг)

Відповідай ТІЛЬКИ у форматі JSON:
{{
  "hard_stop": true/false,
  "hard_stop_reason": "чому саме, або пусто якщо false",
  "stop_list_hit": "назва компанії зі стоп-листа якщо збіг у currentPosition, інакше пусто",
  "ex_stop_list_company": "назва стоп-компанії, якщо кандидат там працював у минулому (для інформації, не hard stop)",
  "location_note": "що саме в location.parsed / location.linkedinText вказує на країну",
  "total_yoe": число,
  "relevant_yoe": число,
  "grade": "Junior/Middle/Senior/Lead",
  "tier": "A/B/C",
  "summary": "2-3 речення українською: чому цей кандидат саме такого тіру для цієї вакансії"
}}
"""
    user_prompt = f"""Опис вакансії:
{job_description}

Дані профілю кандидата (сирий JSON з LinkedIn):
{profile_text}
"""
    return ask_claude_json(client, system_prompt, user_prompt)


# ---------------------------------------------------------------------------
# AGENT 4: SELF-CHECKER (CRITIC)
# ---------------------------------------------------------------------------

def agent_critic(client, job_description: str, profile_text: str, evaluator_result: dict, stop_list: list) -> dict:
    system_prompt = f"""Ти - Critic, скептичний внутрішній аудитор, який перевіряє роботу
іншого AI-агента (Evaluator) і шукає в ній помилки та галюцинації.

Тобі дали висновок Evaluator-а. Постав собі ці конкретні контрольні питання
і чесно дай відповідь по кожному:
1. Чи Evaluator дійсно звірив САМЕ поточну компанію кандидата (поле
   `currentPosition[].companyName`, а не всю історію `experience`) зі
   стоп-листом {stop_list}, чи просто написав "false" навмання? Якщо
   стоп-компанія є лише в минулому досвіді (не поточна) - це НЕ повинно
   бути причиною відхилення, і якщо Evaluator помилково поставив через це
   hard_stop - познач як issue.
2. Чи справді є докази в `location.parsed` / `location.linkedinText`
   профілю, що людина НЕ з Росії (а не просто відсутність згадки про Росію)?
3. Чи relevant_yoe порахований коректно як релевантний досвід, а не
   переплутаний із total_yoe?
4. Чи grade і tier логічно узгоджені з описаним досвідом?

Відповідай ТІЛЬКИ у форматі JSON:
{{
  "pass": true/false,
  "issues": ["список конкретних проблем, якщо є"],
  "confidence": число від 0 до 1
}}

pass = true ставиш ТІЛЬКИ якщо жодних проблем не знайдено (перфектний скор).
Якщо є хоч один сумнів - pass = false.
"""
    user_prompt = f"""Опис вакансії:
{job_description}

Профіль кандидата (сирий JSON):
{profile_text}

Висновок Evaluator-а, який треба перевірити:
{json.dumps(evaluator_result, ensure_ascii=False, indent=2)}
"""
    return ask_claude_json(client, system_prompt, user_prompt)


# ---------------------------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="AI Sourcing Agent - SKELAR", page_icon="🎯", layout="wide")

st.title("🎯 AI Sourcing Agent - SKELAR")
st.caption("Автономний пошук кандидатів: Apify + Ashby (заглушка поки) + Claude агенти")

# --- SIDEBAR: НАЛАШТУВАННЯ ---
with st.sidebar:
    st.header("🔑 API ключі")
    anthropic_api_key = st.text_input("Anthropic API key", type="password")
    apify_token = st.text_input("Apify API token", type="password")
    ashby_api_key = st.text_input(
        "Ashby Admin API key (опційно)",
        type="password",
        help="Поки не обов'язково. Без нього Ashby-перевірка просто пропускається зі статусом 'Не перевірено'.",
    )

    st.divider()
    st.header("⚙️ Параметри циклу")
    max_iterations = st.slider("Максимум ітерацій пошуку (safety limit)", min_value=1, max_value=20, value=5)
    max_profiles_per_iteration = st.slider("Профілів за одну ітерацію (Apify maxItems)", min_value=10, max_value=100, value=25)
    enable_google_xray = st.checkbox(
        "🌐 Додатково шукати через Google X-ray",
        value=True,
        help="Паралельний канал у тій самій ітерації: шукає LinkedIn-профілі через Google (site:linkedin.com/in), "
        "яких могла не показати пряма видача LinkedIn-пошуку. Використовує той самий Apify-токен, окремий ключ не потрібен.",
    )

    st.divider()
    st.header("🚫 Do-Not-Hunt список компаній")
    st.caption("Впиши через кому назви компаній, кандидатів з яких (поточних або минулих) система одразу відсіює.")
    stop_list_raw = st.text_area(
        "Стоп-лист компаній",
        value="Приклад Компанія 1, Приклад Компанія 2",
        height=100,
    )

    st.divider()
    st.header("🌍 Локація / громадянство")
    allow_belarusians = st.checkbox("Дозволити білорусів, якщо вони ЗА кордоном Білорусі", value=False)

# --- ОБРОБКА СТОП-ЛИСТА ---
stop_list = [c.strip() for c in stop_list_raw.split(",") if c.strip()]

# --- ІНІЦІАЛІЗАЦІЯ SESSION STATE ---
# stage: "idle" (форма вводу) -> "active" (крутиться по ітераціях з чекпоінтами) -> "done" (фінал)
for key, default in [
    ("stage", "idle"),
    ("trigger_run", False),
    ("pending_note", ""),
    ("approved_candidates", []),
    ("log", []),
    ("search_history", []),
    ("seen_urls", set()),
    ("iteration", 0),
    ("page_cursor", {}),
]:
    if key not in st.session_state:
        st.session_state[key] = default


def search_signature(search_params: dict) -> str:
    """"Підпис" конкретного пошукового запиту (як cursor.json в еталонному
    скілі /sourcer-ai) - щоб пам'ятати, на якій сторінці видачі ми зупинились
    для КОЖНОГО унікального запиту окремо, а не для пошуку загалом."""
    query = (search_params.get("searchQuery") or "").strip().lower()
    locations = tuple(sorted(l.strip().lower() for l in (search_params.get("locations") or [])))
    return f"{query}|{locations}"


def run_one_iteration(claude_client, apify_token: str, recruiter_note: str):
    """Виконує РІВНО одну ітерацію циклу (Strategist -> Apify -> Evaluator -> Critic)
    і зупиняється. Це навмисно - щоб рекрутер міг подивитись проміжний результат
    і дати текстову корективу перед наступною ітерацією, а не чекати кінця всього
    циклу наосліп."""
    st.session_state.iteration += 1
    iteration = st.session_state.iteration
    still_needed = st.session_state.target_count - len(st.session_state.approved_candidates)
    log = st.session_state.log

    log.append(f"#### Ітерація {iteration}/{st.session_state.max_iterations}")
    if recruiter_note.strip():
        log.append(f"📝 Корективи від рекрутера для цієї ітерації: _{recruiter_note.strip()}_")

    search_params = agent_strategist(
        claude_client, st.session_state.job_description, st.session_state.search_history, still_needed, recruiter_note
    )
    extra_titles_note = f" | точний фільтр тайтлів: {search_params.get('currentJobTitles')}" if search_params.get("currentJobTitles") else ""
    log.append(
        f"🔎 Запит: `{search_params.get('searchQuery')}`{extra_titles_note} | локації: {search_params.get('locations')}"
    )

    # --- PM-цикл: Claude сам діагностує і лагодить запит, якщо перша спроба
    # дала помилку або підозріло 0 профілів, замість того щоб сліпо чекати
    # наступної повної ітерації циклу. ---
    previous_attempts = []
    profiles, apify_error = [], None
    for attempt in range(1, MAX_APIFY_ATTEMPTS_PER_ITERATION + 1):
        sig = search_signature(search_params)
        start_page = st.session_state.page_cursor.get(sig, 1)
        profiles, apify_error = run_apify_search(apify_token, search_params, st.session_state.max_profiles, start_page=start_page)
        # Наступного разу для цього ж самого запиту тягнемо НАСТУПНУ сторінку
        # видачі, а не ту саму - інакше повторно повертається той самий верх.
        pages_consumed = max(1, math.ceil(st.session_state.max_profiles / 25))
        st.session_state.page_cursor[sig] = start_page + pages_consumed

        previous_attempts.append({
            "attempt": attempt, "search_params": search_params,
            "error": apify_error, "profiles_found": len(profiles),
        })

        if apify_error:
            log.append(f"❌ Спроба {attempt}/{MAX_APIFY_ATTEMPTS_PER_ITERATION}: помилка Apify - {apify_error}")
        else:
            log.append(f"📥 Спроба {attempt}/{MAX_APIFY_ATTEMPTS_PER_ITERATION} (сторінка {start_page}): знайдено {len(profiles)} профілів")

        if not apify_error and len(profiles) > 0:
            break  # Все ок - працюємо з цим результатом далі

        if attempt == MAX_APIFY_ATTEMPTS_PER_ITERATION:
            log.append("⏹️ PM-агент вичерпав спроби полагодити запит у цій ітерації, працюю з тим, що є")
            break

        log.append("🛠️ PM-агент аналізує, чому запит не дав результату, і лагодить його...")
        repaired = agent_diagnose_and_repair(
            claude_client, st.session_state.job_description, search_params, apify_error, len(profiles), previous_attempts
        )
        if not repaired:
            log.append("⚠️ PM-агент не зміг запропонувати виправлення, зупиняюсь на поточному запиті")
            break
        log.append(f"🛠️ Діагноз: {repaired.get('diagnosis')}")
        log.append(f"🛠️ Новий запит: `{repaired.get('searchQuery')}` | локації: {repaired.get('locations')}")
        search_params = repaired

    new_profiles = [p for p in profiles if get_profile_url(p) not in st.session_state.seen_urls]
    for p in new_profiles:
        st.session_state.seen_urls.add(get_profile_url(p))
    log.append(f"📥 LinkedIn-пошук (Apify): підсумково {len(profiles)} профілів, з них {len(new_profiles)} нових")

    # Джерело 1: прямий LinkedIn-пошук
    candidate_pool = [(p, "LinkedIn пошук") for p in new_profiles]

    # Джерело 2 (паралельний канал): Google X-ray - той самий Apify-токен,
    # шукає LinkedIn-профілі, яких могла не показати пряма видача LinkedIn.
    if st.session_state.get("enable_google_xray"):
        xray_query = build_xray_query(search_params)
        log.append(f"🌐 Google X-ray запит: `{xray_query}`")
        xray_urls, xray_error = run_google_xray_search(apify_token, xray_query)
        if xray_error:
            log.append(f"❌ ПОМИЛКА Google X-ray пошуку: {xray_error}")
        already_found_urls = st.session_state.seen_urls
        new_xray_urls = [u for u in xray_urls if u not in already_found_urls]
        for u in new_xray_urls:
            st.session_state.seen_urls.add(u)
        log.append(f"🌐 Google X-ray: знайдено {len(xray_urls)} посилань, з них {len(new_xray_urls)} нових")

        xray_profiles, fetch_error = fetch_linkedin_profiles_by_url(apify_token, new_xray_urls)
        if fetch_error:
            log.append(f"❌ ПОМИЛКА дотягування профілів через Google X-ray: {fetch_error}")
        log.append(f"🌐 Google X-ray: дотягнув повні дані для {len(xray_profiles)} профілів")
        candidate_pool += [(p, "LinkedIn через Google X-ray") for p in xray_profiles]

    approved_this_iteration = 0
    for profile, source in candidate_pool:
        if len(st.session_state.approved_candidates) >= st.session_state.target_count:
            break

        name = get_profile_name(profile)
        url = get_profile_url(profile)

        # Agent 2: ATS check (заглушка, поки без реального Ashby)
        ats_result = agent_ats_checker(st.session_state.ashby_api_key, profile)
        if ats_result["status"] == "exists_blocked":
            log.append(f"⛔ {name}: активний на вакансії іншого рекрутера в Ashby, пропускаю")
            continue

        # Agent 3: Strict Evaluator
        profile_text = profile_to_compact_text(profile)
        eval_result = agent_evaluator(
            claude_client, st.session_state.job_description, profile_text, st.session_state.stop_list, st.session_state.allow_belarusians
        )
        if "error" in eval_result:
            log.append(f"⚠️ {name}: помилка оцінки ({eval_result['error']}), пропускаю")
            continue
        if eval_result.get("hard_stop"):
            log.append(f"⛔ {name}: hard stop - {eval_result.get('hard_stop_reason')}")
            continue
        if eval_result.get("tier") != "A":
            log.append(f"🟡 {name}: {eval_result.get('tier', '?')}-tier, не ідеальний збіг, пропускаю")
            continue

        # Agent 4: Critic
        critic_result = agent_critic(claude_client, st.session_state.job_description, profile_text, eval_result, st.session_state.stop_list)
        if "error" in critic_result or not critic_result.get("pass"):
            reason = critic_result.get("issues") if "error" not in critic_result else critic_result["error"]
            log.append(f"⚠️ {name}: не пройшов self-check ({reason}), пропускаю")
            continue

        summary_text = eval_result.get("summary", "")
        ex_stop_company = eval_result.get("ex_stop_list_company")
        if ex_stop_company:
            summary_text += f" (Ex-{ex_stop_company} - колишній працівник стоп-компанії, не поточний)"

        st.session_state.approved_candidates.append({
            "Обрати": False,
            "Ім'я": name,
            "LinkedIn": url,
            "Поточна роль": get_profile_headline(profile),
            "Грейд": eval_result.get("grade", "н/д"),
            "Джерело": source,
            "Ashby статус": ats_result["note"],
            "AI summary": summary_text,
        })
        approved_this_iteration += 1
        log.append(f"✅ {name} ({source}) - додано як A-tier ({len(st.session_state.approved_candidates)}/{st.session_state.target_count})")

    st.session_state.search_history.append({
        "iteration": iteration,
        "search_params": search_params,
        "recruiter_note": recruiter_note,
        "candidates_checked": len(candidate_pool),
        "approved_this_iteration": approved_this_iteration,
        "approved_total": len(st.session_state.approved_candidates),
    })

    if len(st.session_state.approved_candidates) >= st.session_state.target_count:
        st.session_state.stage = "done"
        log.append(f"✅ Ціль досягнута: {len(st.session_state.approved_candidates)}/{st.session_state.target_count}")
    elif iteration >= st.session_state.max_iterations:
        st.session_state.stage = "done"
        log.append(
            f"⏹️ Ліміт ітерацій вичерпано ({st.session_state.max_iterations}), "
            f"знайдено {len(st.session_state.approved_candidates)}/{st.session_state.target_count}"
        )


# --- ЕКРАН 1: СТАРТОВА ФОРМА (тільки поки цикл ще не запущений) ---
if st.session_state.stage == "idle":
    job_description_input = st.text_area(
        "Опис вакансії / вимоги (Job Description)",
        height=250,
        placeholder="Встав сюди повний опис вакансії: тайтл, обов'язки, must-have навички, локація тощо.",
        key="jd_input",
    )
    target_count_input = st.number_input(
        "Скільки ідеальних (A-tier) кандидатів потрібно?", min_value=1, max_value=50, value=10, key="target_input"
    )
    start_button = st.button("🚀 Почати пошук", type="primary")

    if start_button:
        if not anthropic_api_key or not apify_token:
            st.error("Потрібні щонайменше Anthropic API key і Apify API token в лівій панелі.")
            st.stop()
        if not job_description_input.strip():
            st.error("Встав опис вакансії, інакше система не знатиме, кого шукати.")
            st.stop()

        # Фіксуємо параметри запуску - щоб зміни в сайдбарі під час циклу
        # не плутали хід уже розпочатого пошуку
        st.session_state.job_description = job_description_input
        st.session_state.target_count = target_count_input
        st.session_state.max_iterations = max_iterations
        st.session_state.max_profiles = max_profiles_per_iteration
        st.session_state.enable_google_xray = enable_google_xray
        st.session_state.stop_list = stop_list
        st.session_state.allow_belarusians = allow_belarusians
        st.session_state.ashby_api_key = ashby_api_key

        st.session_state.iteration = 0
        st.session_state.approved_candidates = []
        st.session_state.seen_urls = set()
        st.session_state.search_history = []
        st.session_state.log = []
        st.session_state.page_cursor = {}
        st.session_state.stage = "active"
        st.session_state.trigger_run = True
        st.rerun()

# --- ЕКРАН 2: АКТИВНИЙ ЦИКЛ - РІВНО ОДНА ІТЕРАЦІЯ ЗА РАЗ + ЧЕКПОІНТ ---
if st.session_state.stage == "active":
    claude_client = anthropic.Anthropic(api_key=anthropic_api_key)

    if st.session_state.trigger_run:
        with st.spinner(f"Виконую ітерацію {st.session_state.iteration + 1}/{st.session_state.max_iterations}..."):
            run_one_iteration(claude_client, apify_token, st.session_state.pending_note)
        st.session_state.trigger_run = False
        st.session_state.pending_note = ""
        st.rerun()
    else:
        st.info(
            f"Ітерація {st.session_state.iteration}/{st.session_state.max_iterations} завершена. "
            f"Знайдено {len(st.session_state.approved_candidates)}/{st.session_state.target_count} A-tier кандидатів."
        )
        recruiter_note = st.text_area(
            "💬 Корективи для наступної ітерації (необов'язково)",
            placeholder="напр. 'шукай ще в Львові', 'тайтл X не підходить, забудь про нього', 'спробуй компанію Y як донора'",
            key="recruiter_note_input",
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("▶️ Продовжити з наступною ітерацією", type="primary", use_container_width=True):
                st.session_state.pending_note = recruiter_note
                st.session_state.trigger_run = True
                st.rerun()
        with col2:
            if st.button("⏹️ Завершити зараз і показати фінальний список", use_container_width=True):
                st.session_state.stage = "done"
                st.rerun()

# --- ЛОГ ПОШУКУ (видно і під час циклу, і після завершення) ---
if st.session_state.log:
    with st.expander("📜 Лог пошуку по ітераціях", expanded=(st.session_state.stage != "done")):
        for line in st.session_state.log:
            st.markdown(line)

# --- ЕКРАН 3: ФІНАЛЬНІ РЕЗУЛЬТАТИ ---
if st.session_state.stage == "done":
    if 0 < len(st.session_state.approved_candidates) < st.session_state.get("target_count", 0):
        st.warning(
            f"Зупинено на {len(st.session_state.approved_candidates)}/{st.session_state.target_count} кандидатів. "
            "Якщо цього не досить - онови критерії пошуку (стоп-лист, локації, тайтли в JD) і запусти новий пошук."
        )

    if st.session_state.approved_candidates:
        st.divider()
        st.subheader(f"Результати: {len(st.session_state.approved_candidates)} схвалених кандидатів")

        edited = st.data_editor(
            st.session_state.approved_candidates,
            column_config={
                "Обрати": st.column_config.CheckboxColumn("Обрати"),
                "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
            },
            disabled=["Ім'я", "LinkedIn", "Поточна роль", "Грейд", "Джерело", "Ashby статус", "AI summary"],
            use_container_width=True,
            hide_index=True,
        )

        selected = [row for row in edited if row["Обрати"]]

        if st.button(f"📤 Експортувати обраних ({len(selected)}) у CSV для Ashby"):
            if not selected:
                st.warning("Спочатку познач хоча б одного кандидата чекбоксом.")
            else:
                buf = io.StringIO()
                writer = csv.writer(buf)
                # УВАГА: це орієнтовний формат CSV. Точні назви колонок для імпорту
                # треба звірити з реальним Ashby-акаунтом, коли буде доступ до нього.
                writer.writerow(["Name", "LinkedIn URL", "Current Role", "Grade", "Source", "Notes"])
                for row in selected:
                    writer.writerow([row["Ім'я"], row["LinkedIn"], row["Поточна роль"], row["Грейд"], row["Джерело"], row["AI summary"]])
                st.download_button(
                    "⬇️ Завантажити CSV",
                    data=buf.getvalue(),
                    file_name="skelar_sourcing_export.csv",
                    mime="text/csv",
                )

    st.divider()
    if st.button("🔄 Почати новий пошук"):
        for key in ["stage", "trigger_run", "pending_note", "approved_candidates", "log", "search_history", "seen_urls", "iteration", "page_cursor"]:
            st.session_state.pop(key, None)
        st.rerun()
