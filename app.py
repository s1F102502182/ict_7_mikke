"""
Mikke - 本探しアプリ (Google Books + Yahoo!ショッピング 価格比較 + カーリル図書館検索)

ファイル構成:
    app.py                       <- このファイル。ルーティングとAPI呼び出し
    templates/index.html         <- 検索フォームの画面
    templates/display.html       <- 検索結果の一覧画面(価格情報なし・軽量)
    templates/book_detail.html   <- 1冊の詳細画面(価格比較・おすすめ・図書館貸出状況)
    templates/library_setting.html <- ★新規: 図書館の地域を設定する画面

事前準備:
    .envファイルにAPIキーを設定してください(CALIL_APP_KEYを追加)。

実行方法:
    pip install flask requests python-dotenv
    python app.py

--------------------------------------------------------------
■ 今回の変更(★変更点コメントを目印に探してください)
  カーリルAPI(図書館蔵書検索)を追加しました。

  流れ:
    1. ユーザーが /library-setting で都道府県・市区町村を入力
    2. カーリルの /library API で近くの図書館システムを検索し、
       システムIDをセッションに保存する(← 一度設定すれば毎回聞かれない)
    3. 本の詳細ページ(/book/<id>)で、保存されたシステムIDと
       その本のISBNをもとに、カーリルの /check API で貸出状況を問い合わせる

  注意: カーリルの/checkは「非同期」で、一度のリクエストで結果が
  揃わないことがある(continue=1で返ってくる)。プロトタイプ用の割り切りとして、
  最大3回までポーリングし、それでも終わらなければ「取得中」扱いで打ち切る。
--------------------------------------------------------------
"""

from flask import Flask, render_template, request, session, redirect, url_for
import requests
import os
import time
import re
import json  # ★変更点: LLMのツール呼び出し引数(JSON文字列)を解析するために使用
import xml.etree.ElementTree as ET
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "mikke-dev-secret-key-change-later"

MAX_HISTORY = 5
MAX_LIBRARY_SYSTEMS = 3


# ==========================================================
# APIキー設定
# ==========================================================
YAHOO_APP_ID = os.getenv("YAHOO_APP_ID")
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY")
CALIL_APP_KEY = os.getenv("CALIL_APP_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# ★変更点: 楽天ブックスAPI(価格比較に追加するため)
RAKUTEN_APPLICATION_ID = os.getenv("RAKUTEN_APPLICATION_ID")
RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID")

# ★変更点: OpenAI APIのクライアントをここで1回だけ作っておき、使い回す
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _parse_volume_item(item: dict) -> dict:
    """Google Books APIのレスポンス1件分(item)を、アプリ内で使う辞書に変換する。"""
    info = item.get("volumeInfo", {})
    description = info.get("description", "説明なし")
    book_id = item.get("id", "")

    # ISBN_13を優先しつつ、無ければISBN_10もフォールバックとして拾う
    isbn = ""
    isbn_10_fallback = ""
    for identifier in info.get("industryIdentifiers", []):
        if identifier.get("type") == "ISBN_13":
            isbn = identifier.get("identifier", "")
            break
        if identifier.get("type") == "ISBN_10":
            isbn_10_fallback = identifier.get("identifier", "")
    if not isbn:
        isbn = isbn_10_fallback

    return {
        "id": book_id,
        "title": info.get("title", "タイトル不明"),
        "authors": ", ".join(info.get("authors", ["著者不明"])),
        "publisher": info.get("publisher", "出版社不明"),
        "page_count": info.get("pageCount", "不明"),
        "description": description[:150],
        "description_is_truncated": len(description) > 150,
        "thumbnail": info.get("imageLinks", {}).get("thumbnail", ""),
        "published_date": info.get("publishedDate", ""),
        "categories": info.get("categories", []),
        "isbn": isbn,
    }


# ★変更点: 「AIエージェントらしさ」の第一歩。
# ユーザーの曖昧なシチュエーション文("疲れているときに読みたい本"など)を、
# そのままGoogle Books APIに渡すのではなく、
# 一度Claudeに解釈させて「検索に適したキーワード」に変換させる。
# ここがこれまでの「文字列をそのまま右から左に流すだけ」の実装との違い。
def interpret_search_query(user_input: str) -> str:
    """
    OpenAI APIを使い、ユーザーの入力(気分・シチュエーションの説明)を
    書籍検索に適したキーワードに変換する。

    Args:
        user_input: ユーザーが検索窓に入力した文章
                    (例: "疲れているときに読みたい本")

    Returns:
        検索キーワード文字列(例: "癒やし 短編 エッセイ")
        OpenAI APIが使えない/失敗した場合は、元の入力をそのまま返す(フォールバック)
    """
    if openai_client is None:
        # APIキーが未設定の場合は、AIを使わず元の文字列で検索を続行する
        return user_input

    try:
        # ★変更点: OpenAIはsystemメッセージもmessagesのリストの中に含める形式
        # (Claude APIのように独立したsystem引数は使わない)
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # 単純なキーワード変換なので、速くて安価なミニモデルを使用
            max_tokens=50,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたは書籍検索アシスタントです。"
                        "ユーザーが入力した気分やシチュエーションの説明を、"
                        "書籍検索エンジン(Google Books)で良い検索結果が得られるような、"
                        "2〜4語程度の日本語キーワードに変換してください。"
                        "説明や前置き、記号は一切不要です。キーワードのみを出力してください。"
                    ),
                },
                {"role": "user", "content": user_input},
            ],
        )
        # ★変更点: OpenAIのレスポンス構造はchoices[0].message.contentで取得する
        keywords = response.choices[0].message.content.strip()
        return keywords if keywords else user_input
    except Exception as e:
        # OpenAI API呼び出しが何らかの理由で失敗しても、検索自体は続行できるようにする
        print(f"[エラー] OpenAI APIの呼び出しに失敗しました: {e}")
        return user_input


def search_books(query: str, max_results: int = 10) -> list[dict]:
    """Google Books APIで書籍を検索する(一覧用・価格情報は含まない)。"""
    url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": query,
        "maxResults": max_results,
        "key": GOOGLE_BOOKS_API_KEY,
        "printType": "books",
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    return [_parse_volume_item(item) for item in data.get("items", [])]


def get_book_by_id(book_id: str) -> dict | None:
    """Google Books APIで、IDを指定して1冊分の詳細情報を取得する。"""
    url = f"https://www.googleapis.com/books/v1/volumes/{book_id}"
    params = {"key": GOOGLE_BOOKS_API_KEY}

    response = requests.get(url, params=params)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    item = response.json()

    return _parse_volume_item(item)


# ★変更点(案A: 検索エージェント): これまでの interpret_search_query() は
# 「LLMがキーワードを1回考えて、それを固定処理に渡すだけ」だったが、
# ここではLLMに「Google Books検索」というツール(道具)そのものを渡し、
# 何回・どんなキーワードで検索するかをLLM自身に判断させる。
# LLMが「結果を見て次の行動を決める」ループになっている点が、
# これまでの実装との決定的な違い。

# LLMに渡す「道具」の定義。実際の処理はsearch_books()を再利用する。
SEARCH_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_google_books",
            "description": "Google Books APIで書籍をキーワード検索する。日本語の検索キーワードを渡すこと。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索キーワード(2〜4語程度の日本語)",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

# ★変更点: LLMの判断がブレて無限にループしないよう、ここで上限を固定する。
# 「エージェントの自律性」と「暴走を防ぐガードレール」は必ずセットで実装する必要がある。
MAX_AGENT_ITERATIONS = 3
# ユーザーが件数を指定しなかった場合のデフォルト値
DEFAULT_MAX_RESULTS = 10
# 選択肢として画面に表示する件数(index.htmlのプルダウンと対応させる)
ALLOWED_MAX_RESULTS = [5, 10, 15, 20]


def run_search_agent(user_input: str, max_results: int = DEFAULT_MAX_RESULTS) -> tuple[list[dict], list[str]]:
    """
    Function Callingを使い、LLMに検索ツールを渡して自律的に検索させるエージェント。

    LLMは以下を自分で判断する:
        - 最初にどんなキーワードで検索するか
        - 結果が少ない場合、別の言い回しで再検索するかどうか
        - いつ検索を打ち切るか(ただしコード側で最大回数の上限は必ず設ける)

    Args:
        user_input: ユーザーが入力したシチュエーション文
        max_results: ★変更点: ユーザーが指定した「欲しい件数」。
                     LLMに「この件数集まったら終了してよい」と伝える目安にすると同時に、
                     最終的に返す件数の上限としても使う。

    Returns:
        (見つかった本のリスト, LLMが実際に使った検索キーワードのリスト)
        の組。OpenAI APIが使えない場合は、簡易版(キーワード変換1回だけ)にフォールバックする。
    """
    # APIキーが未設定なら、以前作った簡易版(1回だけキーワード変換)にフォールバックする
    if openai_client is None:
        refined = interpret_search_query(user_input)
        try:
            books = search_books(refined, max_results=max_results)
        except requests.exceptions.RequestException:
            books = []
        return books, [refined]

    messages = [
        {
            "role": "system",
            "content": (
                "あなたは本探しエージェントです。ユーザーの気分やシチュエーションの説明から、"
                "search_google_books ツールを使って本を検索してください。"
                f"合計{max_results}件以上の本が集まったら、それ以上ツールを呼ばずに"
                "検索結果に満足した旨を一言述べて終了してください。"
                "1回の検索で件数が少ない場合は、言い回しを変えたキーワードで"
                f"最大{MAX_AGENT_ITERATIONS}回まで検索し直してよいです。"
            ),
        },
        {"role": "user", "content": user_input},
    ]

    collected_books: dict[str, dict] = {}  # book_id -> book。辞書にすることで重複を自動的に除去する
    queries_used: list[str] = []

    for _ in range(MAX_AGENT_ITERATIONS):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=SEARCH_AGENT_TOOLS,
            )
        except Exception as e:
            print(f"[エラー] 検索エージェント(OpenAI API)の呼び出しに失敗しました: {e}")
            break

        choice_message = response.choices[0].message
        messages.append(choice_message)

        # LLMがツールを呼ばずに文章だけ返してきた場合、「検索を終了する」という判断とみなす
        if not choice_message.tool_calls:
            break

        for tool_call in choice_message.tool_calls:
            if tool_call.function.name != "search_google_books":
                continue

            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}
            query = args.get("query", user_input)
            queries_used.append(query)

            try:
                # ★変更点: 1回の検索件数も、欲しい総数を上回りすぎないよう調整する
                results = search_books(query, max_results=min(max_results, 5))
            except requests.exceptions.RequestException:
                results = []

            for book in results:
                collected_books[book["id"]] = book

            # ツール実行結果をLLMに返し、次に何をすべきか判断させる
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"{len(results)}件見つかりました(累計{len(collected_books)}件)。",
                }
            )

        # ★変更点: ユーザーが指定した件数(max_results)に達していたら、終了を促すメッセージを追加する
        if len(collected_books) >= max_results:
            messages.append({"role": "user", "content": "十分な件数が集まりました。検索を終了してください。"})

    # ★変更点: 最終的に返す件数も、ユーザーが指定したmax_resultsに合わせる(以前は固定で10件だった)
    return list(collected_books.values())[:max_results], queries_used


def get_recommendations(categories: list[str], exclude_title: str, max_results: int = 4) -> list[dict]:
    """指定したジャンルに近い本を検索し、「おすすめ」として返す。"""
    if not categories:
        return []

    main_category = categories[0].split("/")[0].strip()
    query = f"subject:{main_category}"

    try:
        candidates = search_books(query, max_results=max_results + 1)
    except requests.exceptions.RequestException:
        return []

    recommendations = [b for b in candidates if b["title"] != exclude_title]

    return recommendations[:max_results]


def search_yahoo_prices(title: str, max_results: int = 3) -> list[dict]:
    """
    Yahoo!ショッピング商品検索API(v3)で、本のタイトルから価格情報を複数件取得する。

    ★変更点: これまでは1件だけ取得していたが、モックアップにあるような
    「複数ショップの価格比較テーブル」を作るため、上位3件を取得し、
    価格の安い順に並び替えて返すよう変更した。

    Args:
        title: 本のタイトル(検索キーワードとして使う)
        max_results: 取得する商品件数

    Returns:
        価格情報の辞書のリスト(価格の安い順)。見つからなければ空リスト。
    """
    url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {
        "appid": YAHOO_APP_ID,
        "query": title,
        "results": max_results,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    hits = data.get("hits", [])

    offers = [
        {
            # ★変更点: 「どのサイトから取得したか」を示すsourceフィールドを追加。
            # 将来、楽天市場やYahoo!ショッピング以外のサイトを追加するとき、
            # この値で見分けられるようにするための布石。
            "source": "Yahoo!ショッピング",
            "price": hit.get("price"),
            "url": hit.get("url", ""),
            "store_name": hit.get("seller", {}).get("name", "店舗不明"),
        }
        for hit in hits
        if hit.get("price") is not None
    ]

    # 価格が安い順に並び替える
    offers.sort(key=lambda offer: offer["price"])

    return offers


# ★変更点: 楽天ブックスAPIから価格情報を取得する関数。
# search_yahoo_prices()と全く同じ形の辞書(source/price/url/store_name)を
# 返すよう揃えてあるので、get_price_offers()側は「何のサイトか」を
# 意識せずそのまま合算・ソートできる。
def search_rakuten_prices(title: str, max_results: int = 3) -> list[dict]:
    """
    楽天ブックス書籍検索APIで、本のタイトルから価格情報を複数件取得する。

    Args:
        title: 本のタイトル(検索キーワードとして使う)
        max_results: 取得する商品件数

    Returns:
        価格情報の辞書のリスト(価格の安い順)。見つからなければ空リスト。
    """
    url = "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404"
    params = {
        "format": "json",
        "title": title,
        "applicationId": RAKUTEN_APPLICATION_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID,
        "hits": max_results,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    items = data.get("Items", [])

    offers = [
        {
            "source": "楽天ブックス",
            "price": item_wrapper.get("Item", {}).get("itemPrice"),
            "url": item_wrapper.get("Item", {}).get("itemUrl", ""),
            # ★変更点: 楽天ブックスは単一の店舗(楽天ブックス自身)からの販売なので、
            # Yahoo!のような複数店舗名の代わりに固定の店舗名を入れる
            "store_name": "楽天ブックス",
        }
        for item_wrapper in items
        if item_wrapper.get("Item", {}).get("itemPrice") is not None
    ]

    offers.sort(key=lambda offer: offer["price"])

    return offers


# ★変更点: 価格比較の「窓口」となる関数。
# book_detail()はこの関数だけを呼べばよく、中でどのサイトを何個問い合わせるかは
# ここに集約される。
def get_price_offers(title: str) -> list[dict]:
    """
    複数の価格情報ソースから商品を取得し、まとめて安い順に返す。

    Args:
        title: 本のタイトル

    Returns:
        価格情報の辞書のリスト(全ソース合算・価格の安い順)
    """
    offers = []

    try:
        offers += search_yahoo_prices(title)
    except requests.exceptions.RequestException as e:
        print(f"[エラー] Yahoo!ショッピングの価格取得に失敗しました: {e}")

    # ★変更点: 予告していた通り、1行追加するだけで楽天ブックスを合算できる
    try:
        offers += search_rakuten_prices(title)
    except requests.exceptions.RequestException as e:
        print(f"[エラー] 楽天ブックスの価格取得に失敗しました: {e}")

    offers.sort(key=lambda offer: offer["price"])

    return offers


# ★変更点: カーリルAPIで、地域から近くの図書館システムを検索する関数
# ★変更点: NDL Search(国立国会図書館サーチ)で書名からISBNを補完検索する関数
# Google Books側にISBNが登録されていない本のための「保険」として使う。
NDL_NAMESPACES = {
    "dc": "http://purl.org/dc/elements/1.1/",
}
# ★変更点: NDL Searchで一度に取得する候補件数。
# 増やすほどISBNが見つかる確率は上がるが、その分レスポンスが少し重くなる。
NDL_SEARCH_CANDIDATES = 5


def search_ndl_isbn(title: str, author: str = "") -> str | None:
    """
    NDL SearchのOpenSearch APIで、書名(・著者名)からISBNを検索する。

    ★変更点: これまでは「候補を1件だけ取得し、その1件にISBNが無ければ諦める」
    という設計だったため、取りこぼしが多かった。
    NDL Searchには古い書誌データ(ISBN制度以前の資料など)も大量に含まれており、
    1件目がISBN非掲載のデータであることは珍しくないため、以下の2点を改善する。
        1. 候補を複数件(NDL_SEARCH_CANDIDATES件)取得する
        2. 1件目に固執せず、ISBNが見つかる候補が出るまで順番に確認する
    また、著者名も分かる場合は検索条件に加え、的外れな本がヒットする確率を下げる。

    Args:
        title: 検索したい本のタイトル
        author: 著者名(分かれば精度が上がる。空文字なら書名のみで検索)

    Returns:
        見つかったISBN(ハイフン除去済み)。見つからなければNone。
    """
    url = "https://ndlsearch.ndl.go.jp/api/opensearch"
    params = {"title": title, "cnt": NDL_SEARCH_CANDIDATES}
    if author:
        # NDL SearchのOpenSearchでは、著者名は"creator"パラメータで絞り込める
        params["creator"] = author

    response = requests.get(url, params=params)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = root.findall(".//item")

    # ★変更点: 1件目だけでなく、全候補を順に見て、最初にISBNが見つかったものを採用する
    for item in items:
        for identifier_elem in item.findall("dc:identifier", NDL_NAMESPACES):
            raw_text = identifier_elem.text or ""
            digits_only = re.sub(r"[^0-9Xx]", "", raw_text)
            if len(digits_only) in (10, 13):
                return digits_only

    return None



def search_library_systems(pref: str, city: str) -> list[dict]:
    """
    カーリルの/library APIで、指定した都道府県・市区町村に近い図書館システムを取得する。

    Args:
        pref: 都道府県名(例: "東京都")
        city: 市区町村名(例: "渋谷区")。空文字でも都道府県だけで検索可能。

    Returns:
        図書館システムの辞書のリスト。例:
        [{"systemid": "Tokyo_Shibuya", "systemname": "渋谷区図書館", ...}, ...]
    """
    url = "https://api.calil.jp/library"
    params = {
        "appkey": CALIL_APP_KEY,
        "pref": pref,
        "format": "json",
        "callback": "no",  # ★修正: 空文字ではJSONPが解除されない。明示的に"no"を指定する必要がある
    }
    if city:
        params["city"] = city

    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


# ★変更点: カーリルAPIで、指定した本が指定した図書館システムで借りられるか調べる関数
def check_library_availability(isbn: str, system_ids: list[str], max_retries: int = 3) -> dict:
    """
    カーリルの/check APIで、本の貸出状況を問い合わせる。

    カーリルの/checkは非同期処理のため、一度のリクエストでは
    結果が揃っていないことがある(continue=1で返る)。
    プロトタイプでは待ち時間を優先し、最大max_retries回までしか
    ポーリングしない(それ以上は「取得中」扱いで打ち切る)。

    Args:
        isbn: 調べたい本のISBN(13桁)
        system_ids: 問い合わせる図書館システムIDのリスト
        max_retries: 最大ポーリング回数

    Returns:
        {"results": {システムID: {"status": ..., "libkey": {...}, "reserveurl": ...}}, "timed_out": bool}
    """
    if not isbn or not system_ids:
        return {"results": {}, "timed_out": False}

    url = "https://api.calil.jp/check"
    params = {
        "appkey": CALIL_APP_KEY,
        "isbn": isbn,
        "systemid": ",".join(system_ids),
        "format": "json",
        "callback": "no",  # ★修正: 空文字ではJSONPが解除されない。明示的に"no"を指定する必要がある
    }

    for attempt in range(max_retries):
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        books = data.get("books", {})
        book_data = books.get(isbn, {})

        if data.get("continue", 0) == 0:
            # 検索完了。図書館システムごとの結果を返す
            return {"results": book_data, "timed_out": False}

        # まだ終わっていない場合、セッションIDを引き継いで少し待ってから再問い合わせ
        params["session"] = data.get("session")
        time.sleep(2)

    # 規定回数のリトライで終わらなかった場合
    return {"results": {}, "timed_out": True}


def add_to_history(query: str) -> None:
    """検索キーワードをセッションに保存する(直近MAX_HISTORY件・重複排除)。"""
    history = session.get("search_history", [])

    if query in history:
        history.remove(query)

    history.insert(0, query)
    session["search_history"] = history[:MAX_HISTORY]


@app.route("/")
def index():
    """トップページ(検索フォーム)を表示する"""
    history = session.get("search_history", [])
    # ★変更点: 現在設定されている地域を画面に表示できるよう渡す
    library_area = session.get("library_area")
    return render_template("index.html", history=history, library_area=library_area)


@app.route("/search")
def search():
    """
    ★変更点(案A): これまでの「1回だけキーワード変換」から、
    Function Callingによる検索エージェント(run_search_agent)に置き換えた。
    LLMが自律的に検索回数・キーワードを判断する。
    価格取得・おすすめ取得は引き続き詳細ページで行う。

    ★変更点: 検索結果の件数をユーザーが選べるようにした。
    """
    query = request.args.get("q", "")

    if not query:
        history = session.get("search_history", [])
        return render_template("index.html", error="検索キーワードを入力してください", history=history)

    # ★変更点: フォームから送られてきた件数を取得する。
    # 数値に変換できない・許可されていない値が来た場合は、デフォルト件数に落ち着かせる
    # (URLを直接書き換えて変な値を送ってくるケースへの対策でもある)
    try:
        max_results = int(request.args.get("max_results", DEFAULT_MAX_RESULTS))
    except ValueError:
        max_results = DEFAULT_MAX_RESULTS

    if max_results not in ALLOWED_MAX_RESULTS:
        max_results = DEFAULT_MAX_RESULTS

    # ★変更点: エージェントに検索を任せる。戻り値は(本のリスト, 使ったキーワードのリスト)
    books, queries_used = run_search_agent(query, max_results=max_results)

    add_to_history(query)

    # ★変更点: 画面に「元の入力」と「AIが実際に使った検索キーワード(複数の場合あり)」を渡し、
    # エージェントが何回・どんなキーワードで検索したかが見えるようにする(透明性のため)
    return render_template(
        "display.html",
        query=query,
        books=books,
        error=None,
        queries_used=queries_used,
        max_results=max_results,  # ★変更点: 画面側で「〇件表示中」のように使えるよう渡す
    )


# ★変更点: 図書館の地域を設定するページ(GET: フォーム表示 / POST: 保存)
@app.route("/library-setting", methods=["GET", "POST"])
def library_setting():
    """
    ユーザーの都道府県・市区町村を受け取り、カーリルAPIで近くの図書館システムを
    検索して、そのシステムIDをセッションに保存する。
    一度設定すれば、以降は本の詳細ページで毎回この地域の図書館が使われる。

    ★変更点: "next"パラメータを受け取れるようにした。
    book_detail()から「未設定だから設定画面へ」と飛ばされてきた場合、
    設定完了後に元の本の詳細ページへ自動で戻れるようにするため。
    """
    # ★変更点: どこから来たか(次にどこへ戻るか)を、フォームの隠しフィールド経由で引き継ぐ
    next_url = request.values.get("next", url_for("index"))

    if request.method == "GET":
        current_systems = session.get("library_systems", [])
        library_area = session.get("library_area")  # ★変更点: 現在の設定地域も渡す
        return render_template("library_setting.html", current_systems=current_systems, error=None, next_url=next_url, library_area=library_area)

    # POST: フォームから送られてきた都道府県・市区町村で図書館システムを検索
    pref = request.form.get("pref", "")
    city = request.form.get("city", "")

    if not pref:
        return render_template("library_setting.html", current_systems=[], error="都道府県を入力してください", next_url=next_url)

    try:
        libraries = search_library_systems(pref, city)
    except requests.exceptions.RequestException as e:
        return render_template("library_setting.html", current_systems=[], error=str(e), next_url=next_url)

    # 同じsystemidが複数の図書館(分館)に対応していることがあるため、重複を除去
    seen = set()
    unique_systems = []
    for lib in libraries:
        sid = lib.get("systemid")
        if sid and sid not in seen:
            seen.add(sid)
            unique_systems.append({"systemid": sid, "systemname": lib.get("systemname", sid)})

    selected_systems = unique_systems[:MAX_LIBRARY_SYSTEMS]
    session["library_systems"] = selected_systems
    # ★変更点: 「今どこの地域が設定されているか」を画面に表示できるよう、
    # 地域名(都道府県・市区町村)自体もセッションに保存しておく
    session["library_area"] = {"pref": pref, "city": city}

    # ★変更点: 保存が完了したら、確認画面を挟まずに元のページ(next_url)へ自動で戻る
    return redirect(next_url)


@app.route("/book/<book_id>")
def book_detail(book_id):
    """
    1冊分の詳細情報・価格比較・おすすめ・図書館貸出状況を表示する。
    """
    # 強制リダイレクトの処理を削除しました

    try:
        book = get_book_by_id(book_id)
    except requests.exceptions.RequestException as e:
        return render_template("book_detail.html", book=None, error=str(e), price_offers=[], recommendations=[], library_results=None, has_isbn=False)

    if book is None:
        return render_template("book_detail.html", book=None, error="本が見つかりませんでした", price_offers=[], recommendations=[], library_results=None, has_isbn=False)

    # ★変更点: 個別のsearch_yahoo_prices()ではなく、窓口関数get_price_offers()を呼ぶ
    try:
        price_offers = get_price_offers(book["title"])
    except requests.exceptions.RequestException:
        price_offers = []

    recommendations = get_recommendations(
        categories=book["categories"],
        exclude_title=book["title"],
    )

    # ★変更点: Google Books側にISBNがなかった場合、NDL Searchで補完を試みる
    if not book["isbn"]:
        try:
            first_author = book["authors"].split(",")[0].strip()
            ndl_isbn = search_ndl_isbn(book["title"], author=first_author)

            # ★変更点: 著者名で絞り込むと、NDL側の表記ゆれ(全角/半角など)で
            # 逆にヒットしなくなることがあるため、失敗したら著者名なしでも再試行する
            if not ndl_isbn:
                ndl_isbn = search_ndl_isbn(book["title"])

            if ndl_isbn:
                book["isbn"] = ndl_isbn
        except (requests.exceptions.RequestException, ET.ParseError) as e:
            print(f"[エラー] NDL Search呼び出しに失敗しました: {e}")

    # ★変更点: ここに来た時点で図書館は必ず設定済みなので、そのまま貸出状況を問い合わせる
    library_results = None
    library_systems = session.get("library_systems", [])
    if library_systems and book["isbn"]:
        system_ids = [lib["systemid"] for lib in library_systems]
        try:
            check_result = check_library_availability(book["isbn"], system_ids)
            system_name_map = {lib["systemid"]: lib["systemname"] for lib in library_systems}

            # ★変更点: libkeyには「個別の図書館名: 貸出状況」の辞書が入っている
            # 例: {"中央図書館": "貸出中", "○○分館": "蔵書あり"}
            # これをsystemname(渋谷区図書館 等)ごとにまとめて、館名単位の一覧を作る
            branches = []
            for sid, info in check_result["results"].items():
                system_name = system_name_map.get(sid, sid)
                libkey = info.get("libkey", {})
                # ★修正: info["status"]はAPI呼び出し自体の状態(OK/Cache/Running/Error)であり、
                # 「本が借りられるか」の意味ではない。これまではlibkeyが空のとき
                # このAPIステータスをそのまま貸出状況として表示してしまっていたため、
                # 「蔵書がない本」でも紛らわしく"OK"や"Cache"と出るバグになっていた。
                api_call_status = info.get("status", "")

                if libkey:
                    # 個別の図書館名・貸出状況が取れた場合は、館ごとに1行ずつ追加する
                    # (libkeyの値自体が「貸出可」「蔵書あり」「貸出中」等の本当の貸出状況)
                    for branch_name, branch_status in libkey.items():
                        branches.append({
                            "name": f"{system_name} {branch_name}",
                            "status": branch_status,
                            "reserveurl": info.get("reserveurl", ""),
                        })
                elif api_call_status == "Error":
                    # API呼び出し自体が失敗した場合のみ、その旨を表示する
                    branches.append({
                        "name": system_name,
                        "status": "取得エラー",
                        "reserveurl": "",
                    })
                else:
                    # ★修正: libkeyが空 = その図書館システムには蔵書が無いという意味なので、
                    # APIステータスではなく「蔵書なし」と明示する
                    branches.append({
                        "name": system_name,
                        "status": "蔵書なし",
                        "reserveurl": "",
                    })

            library_results = {
                "timed_out": check_result["timed_out"],
                # ★修正: キー名を"items"から"libraries"に変更。
                # Jinja2テンプレート内で辞書.itemsと書くと、辞書のキーではなく
                # Python標準の dict.items()メソッド自体を指してしまい、
                # TypeError: object of type 'builtin_function_or_method' has no len()
                # というエラーになるため。
                "libraries": branches,
            }
        except requests.exceptions.RequestException as e:
            print(f"[エラー] カーリルAPI呼び出しに失敗しました: {e}")
            library_results = None

    return render_template(
        "book_detail.html",
        book=book,
        error=None,
        price_offers=price_offers,
        recommendations=recommendations,
        library_results=library_results,
        has_isbn=bool(book["isbn"]),
        library_area=session.get("library_area"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)