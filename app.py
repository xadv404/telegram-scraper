import os
import asyncio
import threading
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from dotenv import load_dotenv
from database import Session, Group, Member, GroupMember, ScrapeJob, Account
from scraper import TelegramScraper
from user_lookup import lookup_user_groups
from categories import CATEGORIES, get_category_seeds
from member_export import export_group_members, list_forum_topics, export_topic_senders

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret")

active_scrapers: dict[int, TelegramScraper] = {}


def load_accounts_from_env() -> list[dict]:
    accounts = []
    for i in range(1, 10):
        api_id = os.getenv(f"ACCOUNT_{i}_API_ID", "").strip()
        api_hash = os.getenv(f"ACCOUNT_{i}_API_HASH", "").strip()
        session = os.getenv(f"ACCOUNT_{i}_SESSION", "").strip()
        if api_id and api_hash and session:
            accounts.append({"api_id": api_id, "api_hash": api_hash, "session": session})
    return accounts


def run_scraper_async(scraper: TelegramScraper, seed_group: str, category_seeds=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scraper.run(seed_group, category_seeds=category_seeds))
    finally:
        loop.close()


@app.route("/")
def index():
    db = Session()
    try:
        jobs = db.query(ScrapeJob).order_by(ScrapeJob.started_at.desc()).limit(10).all()
        groups = db.query(Group).order_by(Group.scraped_at.desc().nullslast()).limit(20).all()
        total_groups = db.query(Group).count()
        total_members = db.query(Member).count()
        accounts = load_accounts_from_env()

        # Stats par catégorie
        from sqlalchemy import func
        cat_stats = db.query(Group.category, func.count(Group.id)).group_by(Group.category).all()
        cat_counts = {r[0]: r[1] for r in cat_stats}

        return render_template(
            "index.html",
            jobs=jobs,
            groups=groups,
            total_groups=total_groups,
            total_members=total_members,
            accounts_count=len(accounts),
            categories=CATEGORIES,
            cat_counts=cat_counts,
        )
    finally:
        db.close()


@app.route("/start", methods=["POST"])
def start_scrape():
    seed = request.form.get("seed_group", "").strip().lstrip("@")
    selected_cats = request.form.getlist("categories")  # catégories cochées

    accounts = load_accounts_from_env()
    if not accounts:
        return jsonify({"error": "Aucun compte configuré dans .env"}), 400

    if not seed and not selected_cats:
        return jsonify({"error": "Donnez un groupe ou sélectionnez des catégories"}), 400

    # Construire les requêtes mots-clés pour les catégories sélectionnées
    all_seeds = get_category_seeds()
    category_queries = []
    for cat_id in selected_cats:
        category_queries.extend(all_seeds.get(cat_id, []))

    seed_label = seed or ("catégories: " + ", ".join(selected_cats))

    db = Session()
    try:
        job = ScrapeJob(seed_group=seed_label, status="running")
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    scraper = TelegramScraper(accounts, job_id)
    active_scrapers[job_id] = scraper

    t = threading.Thread(
        target=run_scraper_async,
        args=(scraper, seed, category_queries if category_queries else None),
        daemon=True,
    )
    t.start()

    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/job/<int:job_id>")
def job_detail(job_id: int):
    db = Session()
    try:
        job = db.query(ScrapeJob).get(job_id)
        if not job:
            return "Job introuvable", 404
        groups = db.query(Group).order_by(Group.depth, Group.scraped_at.desc().nullslast()).all()
        return render_template("job.html", job=job, groups=groups)
    finally:
        db.close()


@app.route("/job/<int:job_id>/status")
def job_status(job_id: int):
    db = Session()
    try:
        job = db.query(ScrapeJob).get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "status": job.status,
            "groups_found": job.groups_found,
            "members_found": job.members_found,
            "current_group": job.current_group,
            "log": (job.log or "").split("\n")[-30:],
        })
    finally:
        db.close()


@app.route("/job/<int:job_id>/stop", methods=["POST"])
def stop_job(job_id: int):
    scraper = active_scrapers.get(job_id)
    if scraper:
        scraper.stop()
        db = Session()
        try:
            job = db.query(ScrapeJob).get(job_id)
            if job:
                job.status = "stopped"
                job.finished_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/categories")
def categories_view():
    db = Session()
    try:
        cat_filter = request.args.get("cat")
        q = db.query(Group)
        if cat_filter:
            q = q.filter(Group.category == cat_filter)
        groups = q.order_by(Group.member_count.desc().nullslast()).all()

        from sqlalchemy import func
        cat_stats = db.query(Group.category, func.count(Group.id)).group_by(Group.category).all()
        cat_counts = {r[0]: r[1] for r in cat_stats}

        return render_template(
            "categories.html",
            groups=groups,
            categories=CATEGORIES,
            cat_counts=cat_counts,
            cat_filter=cat_filter,
        )
    finally:
        db.close()


@app.route("/groups")
def groups_list():
    db = Session()
    try:
        depth = request.args.get("depth", type=int)
        status = request.args.get("status")
        q = db.query(Group)
        if depth is not None:
            q = q.filter(Group.depth == depth)
        if status:
            q = q.filter(Group.status == status)
        groups = q.order_by(Group.depth, Group.member_count.desc().nullslast()).all()
        return render_template("groups.html", groups=groups)
    finally:
        db.close()


@app.route("/group/<username>")
def group_detail(username: str):
    db = Session()
    try:
        group = db.query(Group).filter_by(username=username).first()
        if not group:
            return "Groupe introuvable", 404
        links = db.query(GroupMember).filter_by(group_username=username).all()
        member_ids = [l.member_telegram_id for l in links]
        members = db.query(Member).filter(Member.telegram_id.in_(member_ids)).all()
        return render_template("group_detail.html", group=group, members=members)
    finally:
        db.close()


@app.route("/members")
def members_list():
    db = Session()
    try:
        search = request.args.get("q", "").strip()
        q = db.query(Member)
        if search:
            q = q.filter(Member.username.ilike(f"%{search}%"))
        members = q.order_by(Member.discovered_at.desc()).limit(200).all()
        return render_template("members.html", members=members, search=search)
    finally:
        db.close()


@app.route("/export/groups")
def export_groups():
    db = Session()
    try:
        groups = db.query(Group).all()
        data = [
            {
                "username": g.username,
                "title": g.title,
                "member_count": g.member_count,
                "depth": g.depth,
                "status": g.status,
                "scraped_at": g.scraped_at.isoformat() if g.scraped_at else None,
            }
            for g in groups
        ]
        from flask import Response
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=groups.json"},
        )
    finally:
        db.close()


@app.route("/export-members")
def export_members_page():
    return render_template("export_members.html")


@app.route("/export-members/topics")
def export_members_topics():
    """Retourne la liste des topics d'une communauté (JSON)."""
    username = request.args.get("username", "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username manquant"}), 400
    accounts = load_accounts_from_env()
    if not accounts:
        return jsonify({"error": "Aucun compte configuré"}), 400

    async def fetch():
        return await list_forum_topics(username, accounts)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()


@app.route("/export-members/run")
def export_members_run():
    """SSE : stream la progression puis envoie le résultat JSON final."""
    username = request.args.get("username", "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username manquant"}), 400

    accounts = load_accounts_from_env()
    if not accounts:
        return jsonify({"error": "Aucun compte configuré"}), 400

    import queue as qmod

    def generate():
        q = qmod.Queue()
        result_holder = {}

        def cb(msg):
            q.put({"type": "log", "msg": msg})

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    export_group_members(username, accounts, progress_cb=cb)
                )
                # Envoie les infos groupe dès qu'on les a
                q.put({"type": "group_info", **res["group"]})
                result_holder["data"] = res
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                q.put(None)
                loop.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

        if "error" in result_holder:
            yield f"data: {json.dumps({'type': 'error', 'msg': result_holder['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'result', 'data': result_holder.get('data', {})})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/export-members/run-topic")
def export_members_run_topic():
    """SSE : stream les expéditeurs uniques d'un topic de forum."""
    username = request.args.get("username", "").strip().lstrip("@")
    topic_id = request.args.get("topic_id", type=int)
    topic_title = request.args.get("topic_title", "").strip()

    if not username or not topic_id:
        return jsonify({"error": "username et topic_id requis"}), 400

    accounts = load_accounts_from_env()
    if not accounts:
        return jsonify({"error": "Aucun compte configuré"}), 400

    import queue as qmod

    def generate():
        q = qmod.Queue()
        result_holder = {}

        def cb(msg):
            q.put({"type": "log", "msg": msg})

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    export_topic_senders(username, topic_id, topic_title, accounts, progress_cb=cb)
                )
                q.put({"type": "group_info", **res["group"]})
                result_holder["data"] = res
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                q.put(None)
                loop.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

        if "error" in result_holder:
            yield f"data: {json.dumps({'type': 'error', 'msg': result_holder['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'result', 'data': result_holder.get('data', {})})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/lookup")
def lookup_page():
    return render_template("lookup.html")


@app.route("/lookup/run")
def lookup_run():
    """
    Endpoint SSE (Server-Sent Events) : stream les logs en temps réel
    puis envoie le résultat JSON final.
    """
    username = request.args.get("username", "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username manquant"}), 400

    accounts = load_accounts_from_env()
    if not accounts:
        return jsonify({"error": "Aucun compte configuré"}), 400

    def generate():
        log_lines = []
        result_holder = {}

        def on_progress(msg: str):
            log_lines.append(msg)
            yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"

        # On ne peut pas yield depuis un callback ; on utilise une queue
        import queue as qmod
        q = qmod.Queue()

        def cb(msg):
            q.put(msg)

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    lookup_user_groups(username, accounts, progress_cb=cb)
                )
                result_holder["data"] = res
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                q.put(None)  # sentinel
                loop.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        while True:
            msg = q.get()
            if msg is None:
                break
            yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"

        if "error" in result_holder:
            yield f"data: {json.dumps({'type': 'error', 'msg': result_holder['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'result', 'data': result_holder.get('data', {})})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/export/members")
def export_members():
    db = Session()
    try:
        members = db.query(Member).all()
        data = [
            {
                "telegram_id": m.telegram_id,
                "username": m.username,
                "first_name": m.first_name,
                "last_name": m.last_name,
            }
            for m in members
        ]
        from flask import Response
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=members.json"},
        )
    finally:
        db.close()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
