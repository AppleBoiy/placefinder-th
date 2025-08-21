import os
import json
import urllib.request
import re
from difflib import SequenceMatcher
from typing import Dict, List

from flask import Flask, jsonify, request, render_template, redirect, url_for
from flask_cors import CORS
from flask_restx import Api, Resource


def fetch_and_build_mapping(data_url: str) -> Dict[str, List[dict]]:
    with urllib.request.urlopen(data_url, timeout=15) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        payload = resp.read().decode(charset)
        rows = json.loads(payload)

    zipcode_to_locations: Dict[str, List[dict]] = {}
    for record in rows:
        code = str(record.get("zipCode", "")).zfill(5)
        if not code.isdigit() or len(code) != 5:
            continue

        district_id_to_name = {d.get("districtId"): d.get("districtName") for d in record.get("districtList", [])}
        province_id_to_name = {p.get("provinceId"): p.get("provinceName") for p in record.get("provinceList", [])}

        # Some datasets list a single province per zip; pick any present province name
        province_name = None
        if province_id_to_name:
            # take first province name deterministically by sorted key
            for key in sorted(province_id_to_name.keys()):
                province_name = province_id_to_name[key]
                break

        unique_locations = set()
        for sd in record.get("subDistrictList", []):
            sd_name = sd.get("subDistrictName")
            d_id = sd.get("districtId")
            amphoe_name = district_id_to_name.get(d_id)
            if not sd_name or not amphoe_name:
                continue
            unique_locations.add((province_name, amphoe_name, sd_name))

        for province_name_val, amphoe_name_val, district_name_val in sorted(unique_locations):
            zipcode_to_locations.setdefault(code, []).append({
                "province": province_name_val,
                "amphoe": amphoe_name_val,
                "district": district_name_val,
            })

    return zipcode_to_locations


app = Flask(__name__)
CORS(app)
api = Api(app, title="Thai Zipcode Placefinder API", version="1.0", doc="/docs")

DATA_URL = os.getenv(
    "DATA_URL",
    "https://gist.githubusercontent.com/AppleBoiy/43d87e39f7b99825bb2cfdc45cd568cc/raw/566c534810dc45cd173678e1181c151a33412fe8/th-address.json",
)
ZIPCODE_TO_LOCATIONS = fetch_and_build_mapping(DATA_URL)


def normalize_text(text: str) -> str:
    if text is None:
        return ""
    # Lowercase + collapse whitespace
    return re.sub(r"\s+", " ", str(text).strip().lower())


def is_match(candidate: str, query: str, fuzzy: bool) -> bool:
    c = normalize_text(candidate)
    q = normalize_text(query)
    if not q:
        return True
    if q in c:
        return True
    if fuzzy:
        return SequenceMatcher(None, c, q).ratio() >= 0.75
    return False


# Flatten to records for flexible searching
RECORDS = []
for code, locs in ZIPCODE_TO_LOCATIONS.items():
    for loc in locs:
        RECORDS.append({
            "code": code,
            "province": loc.get("province"),
            "amphoe": loc.get("amphoe"),
            "district": loc.get("district"),
            "province_n": normalize_text(loc.get("province")),
            "amphoe_n": normalize_text(loc.get("amphoe")),
            "district_n": normalize_text(loc.get("district")),
        })

UNIQUE_PROVINCES = sorted({r["province"] for r in RECORDS if r.get("province")})
UNIQUE_AMPHOES = sorted({r["amphoe"] for r in RECORDS if r.get("amphoe")})
UNIQUE_DISTRICTS = sorted({r["district"] for r in RECORDS if r.get("district")})
UNIQUE_ZIPCODES = sorted(ZIPCODE_TO_LOCATIONS.keys())


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok"}), 200


@app.before_request
def _serve_home_for_root():
    if request.path == "/" and request.method in {"GET", "HEAD"}:
        return render_template("home.html")


@app.get("/")
def home_page():
    return render_template("home.html")


@app.route("/ui/zipcode", methods=["GET", "POST"])
def ui_zipcode():
    code = (request.values.get("code") or "").strip()
    data = None
    error = None
    if code:
        if not code.isdigit() or len(code) != 5:
            error = "Invalid zipcode. Must be 5 digits."
        else:
            locs = ZIPCODE_TO_LOCATIONS.get(code, [])
            if not locs:
                error = "Zipcode not found."
            else:
                data = {"code": code, "locations": locs}
    return render_template("zipcode.html", code=code, data=data, error=error)


@app.route("/ui/search", methods=["GET", "POST"])
def ui_search():
    params = {
        "q": request.values.get("q", ""),
        "code": request.values.get("code", ""),
        "province": request.values.get("province", ""),
        "amphoe": request.values.get("amphoe", ""),
        "district": request.values.get("district", ""),
        "fuzzy": request.values.get("fuzzy", "false"),
    }
    results = None
    total = 0
    if any(params.values()):
        # Reuse search logic
        fuzzy = params["fuzzy"].lower() in {"1", "true", "yes", "y"}
        rows = []
        code_q = (params["code"] or "").strip()
        for r in RECORDS:
            if code_q and not r["code"].startswith(code_q):
                continue
            if params["province"] and not is_match(r.get("province", ""), params["province"], fuzzy):
                continue
            if params["amphoe"] and not is_match(r.get("amphoe", ""), params["amphoe"], fuzzy):
                continue
            if params["district"] and not is_match(r.get("district", ""), params["district"], fuzzy):
                continue
            if params["q"]:
                if not (
                    is_match(r.get("province", ""), params["q"], fuzzy)
                    or is_match(r.get("amphoe", ""), params["q"], fuzzy)
                    or is_match(r.get("district", ""), params["q"], fuzzy)
                    or r["code"].startswith(params["q"])
                ):
                    continue
            rows.append({"code": r["code"], "province": r.get("province"), "amphoe": r.get("amphoe"), "district": r.get("district")})
        total = len(rows)
        results = rows[:100]
    return render_template("search.html", params=params, total=total, results=results)


@app.route("/ui/reverse", methods=["GET", "POST"])
def ui_reverse():
    params = {
        "province": request.values.get("province", ""),
        "amphoe": request.values.get("amphoe", ""),
        "district": request.values.get("district", ""),
        "fuzzy": request.values.get("fuzzy", "false"),
    }
    results = None
    if any(params.values()):
        fuzzy = params["fuzzy"].lower() in {"1", "true", "yes", "y"}
        grouped: Dict[str, List[dict]] = {}
        for r in RECORDS:
            if params["province"] and not is_match(r.get("province", ""), params["province"], fuzzy):
                continue
            if params["amphoe"] and not is_match(r.get("amphoe", ""), params["amphoe"], fuzzy):
                continue
            if params["district"] and not is_match(r.get("district", ""), params["district"], fuzzy):
                continue
            grouped.setdefault(r["code"], []).append({"province": r.get("province"), "amphoe": r.get("amphoe"), "district": r.get("district")})
        results = sorted(grouped.items())[:100]
    return render_template("reverse.html", params=params, results=results)


@app.route("/ui/suggest", methods=["GET", "POST"])
def ui_suggest():
    q = request.values.get("q", "")
    limit = 10
    suggestions = None
    if q:
        qn = normalize_text(q)
        def filter_values(values):
            picks = []
            for v in values:
                if normalize_text(v).find(qn) != -1:
                    picks.append(v)
                if len(picks) >= limit:
                    break
            return picks
        zip_sug = [z for z in UNIQUE_ZIPCODES if z.startswith(qn)][:limit]
        prov_sug = filter_values(UNIQUE_PROVINCES)
        amph_sug = filter_values(UNIQUE_AMPHOES)
        dist_sug = filter_values(UNIQUE_DISTRICTS)
        suggestions = {"zipcodes": zip_sug, "provinces": prov_sug, "amphoes": amph_sug, "districts": dist_sug}
    return render_template("suggest.html", q=q, suggestions=suggestions)


# Swagger parsers for query parameters shown in UI
search_parser = api.parser()
search_parser.add_argument("q", type=str, required=False, location="args", help="Free-text query (province/district/subdistrict or zipcode prefix)")
search_parser.add_argument("code", type=str, required=False, location="args", help="Zipcode filter (prefix match)")
search_parser.add_argument("province", type=str, required=False, location="args", help="Province name")
search_parser.add_argument("amphoe", type=str, required=False, location="args", help="District (amphoe/khet) name")
search_parser.add_argument("district", type=str, required=False, location="args", help="Subdistrict (tambon/khwaeng) name")
search_parser.add_argument("fuzzy", type=str, required=False, location="args", help="Enable fuzzy matching: true/false", default="false")
search_parser.add_argument("limit", type=int, required=False, location="args", help="Maximum results to return", default=50)
search_parser.add_argument("offset", type=int, required=False, location="args", help="Results offset (pagination)", default=0)

reverse_parser = api.parser()
reverse_parser.add_argument("province", type=str, required=False, location="args", help="Province name")
reverse_parser.add_argument("amphoe", type=str, required=False, location="args", help="District (amphoe/khet) name")
reverse_parser.add_argument("district", type=str, required=False, location="args", help="Subdistrict (tambon/khwaeng) name")
reverse_parser.add_argument("fuzzy", type=str, required=False, location="args", help="Enable fuzzy matching: true/false", default="false")

suggest_parser = api.parser()
suggest_parser.add_argument("q", type=str, required=False, location="args", help="Query to get suggestions for")
suggest_parser.add_argument("limit", type=int, required=False, location="args", help="Maximum suggestions", default=10)


@api.route("/api/zipcode/<string:code>")
class ZipcodeLookup(Resource):
    def get(self, code: str):
        code_clean = (code or "").strip()
        if not code_clean.isdigit() or len(code_clean) != 5:
            return {"error": "Invalid zipcode. Must be 5 digits."}, 400
        locations = ZIPCODE_TO_LOCATIONS.get(code_clean, [])
        if not locations:
            return {"error": "Zipcode not found in dataset.", "code": code_clean}, 404
        return {"code": code_clean, "locations": locations}, 200


@api.route("/api/search")
class ForwardSearch(Resource):
    @api.expect(search_parser, validate=False)
    def get(self):
        code = request.args.get("code", default="")
        province = request.args.get("province", default="")
        amphoe = request.args.get("amphoe", default="")
        district = request.args.get("district", default="")
        q = request.args.get("q", default="")
        fuzzy = request.args.get("fuzzy", default="false").lower() in {"1", "true", "yes", "y"}
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0

        results = []
        code_q = (code or "").strip()
        for r in RECORDS:
            if code_q:
                if not r["code"].startswith(code_q):
                    continue
            if province and not is_match(r.get("province", ""), province, fuzzy):
                continue
            if amphoe and not is_match(r.get("amphoe", ""), amphoe, fuzzy):
                continue
            if district and not is_match(r.get("district", ""), district, fuzzy):
                continue
            if q:
                if not (
                    is_match(r.get("province", ""), q, fuzzy)
                    or is_match(r.get("amphoe", ""), q, fuzzy)
                    or is_match(r.get("district", ""), q, fuzzy)
                    or r["code"].startswith(q)
                ):
                    continue
            results.append({
                "code": r["code"],
                "province": r.get("province"),
                "amphoe": r.get("amphoe"),
                "district": r.get("district"),
            })

        total = len(results)
        paged = results[offset: offset + limit]
        return {"total": total, "limit": limit, "offset": offset, "results": paged}, 200


@api.route("/api/reverse")
class ReverseLookup(Resource):
    @api.expect(reverse_parser, validate=False)
    def get(self):
        province = request.args.get("province", default="")
        amphoe = request.args.get("amphoe", default="")
        district = request.args.get("district", default="")
        fuzzy = request.args.get("fuzzy", default="false").lower() in {"1", "true", "yes", "y"}

        grouped: Dict[str, List[dict]] = {}
        for r in RECORDS:
            if province and not is_match(r.get("province", ""), province, fuzzy):
                continue
            if amphoe and not is_match(r.get("amphoe", ""), amphoe, fuzzy):
                continue
            if district and not is_match(r.get("district", ""), district, fuzzy):
                continue
            grouped.setdefault(r["code"], []).append({
                "province": r.get("province"),
                "amphoe": r.get("amphoe"),
                "district": r.get("district"),
            })

        if not grouped:
            return {"error": "No results found."}, 404
        # Convert to array form similar to zipcode lookup
        results = [{"code": code, "locations": locs} for code, locs in sorted(grouped.items())]
        return {"results": results}, 200


@api.route("/api/suggest")
class Suggest(Resource):
    @api.expect(suggest_parser, validate=False)
    def get(self):
        q = request.args.get("q", default="")
        try:
            limit = max(1, min(20, int(request.args.get("limit", 10))))
        except ValueError:
            limit = 10

        qn = normalize_text(q)
        if not qn:
            return {
                "zipcodes": UNIQUE_ZIPCODES[:limit],
                "provinces": UNIQUE_PROVINCES[:limit],
                "amphoes": UNIQUE_AMPHOES[:limit],
                "districts": UNIQUE_DISTRICTS[:limit],
            }, 200

        def filter_values(values):
            picks = []
            for v in values:
                if normalize_text(v).find(qn) != -1:
                    picks.append(v)
                if len(picks) >= limit:
                    break
            return picks

        zip_sug = [z for z in UNIQUE_ZIPCODES if z.startswith(qn)][:limit]
        prov_sug = filter_values(UNIQUE_PROVINCES)
        amph_sug = filter_values(UNIQUE_AMPHOES)
        dist_sug = filter_values(UNIQUE_DISTRICTS)
        return {
            "zipcodes": zip_sug,
            "provinces": prov_sug,
            "amphoes": amph_sug,
            "districts": dist_sug,
        }, 200

@app.get("/routes")
def list_routes():
    return jsonify(sorted([f"{r.rule} -> {','.join(r.methods)}" for r in app.url_map.iter_rules()]))

