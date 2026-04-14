import os

from flask import Flask, abort, render_template, request

from data import VEHICLES

app = Flask(__name__)


def filtered_vehicles(year=None, category=None, keyword=None):
    items = VEHICLES
    if year:
        items = [v for v in items if v["year"] == year]
    if category:
        items = [v for v in items if v["category"] == category]
    if keyword:
        keyword_lower = keyword.lower()
        items = [
            v
            for v in items
            if keyword_lower in v["brand"].lower()
            or keyword_lower in v["model"].lower()
        ]
    return items


@app.route("/")
def index():
    year = request.args.get("year", type=int)
    category = request.args.get("category", type=str)
    keyword = request.args.get("q", default="", type=str).strip()

    vehicles = filtered_vehicles(year=year, category=category, keyword=keyword)
    brands = sorted({v["brand"] for v in vehicles})
    return render_template(
        "index.html",
        vehicles=vehicles,
        years=[2025, 2026],
        categories=["SUV", "MPV"],
        selected_year=year,
        selected_category=category,
        keyword=keyword,
        brands=brands,
    )


@app.route("/vehicle/<vehicle_id>")
def vehicle_detail(vehicle_id):
    vehicle = next((v for v in VEHICLES if v["id"] == vehicle_id), None)
    if not vehicle:
        abort(404)
    return render_template("vehicle.html", vehicle=vehicle)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

