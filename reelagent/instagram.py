"""Official Instagram Graph API publishing (Reels), via Facebook Login for Business.

Flow (Meta's resumable upload, no public hosting needed):
  1. POST /{ig-user-id}/media  media_type=REELS upload_type=resumable  -> container id
  2. POST rupload.facebook.com/ig-api-upload/{ver}/{container-id}  (the video bytes)
  3. GET  /{container-id}?fields=status_code  until FINISHED
  4. POST /{ig-user-id}/media_publish creation_id=...
Only the official API is used: no browser bots, no password sharing, nothing that
risks an action block.
"""
from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

import requests

from .config import update_env


class IGError(RuntimeError):
    pass


class IGRateLimited(IGError):
    pass


class IGFileRefused(IGError):
    """Instagram took the request but refused the video itself (rupload ProcessingFailedError, or the
    container ended in ERROR)."""
    pass


def _check(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    err = data.get("error") if isinstance(data, dict) else None
    if r.status_code >= 400 or err or (isinstance(data, dict) and data.get("debug_info")):
        err = err or data.get("debug_info") or data
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        sub = err.get("error_subcode") if isinstance(err, dict) else None
        if isinstance(err, dict) and err.get("type") == "ProcessingFailedError":
            raise IGFileRefused(f"Instagram refused the video file ({msg})")
        if code in (4, 9, 17, 32, 613) or sub == 2207042:
            raise IGRateLimited(f"Instagram rate limit: {msg}")
        if code == 190:
            raise IGError(f"Instagram token invalid/expired: {msg}. Run: python -m reelagent setup-instagram")
        raise IGError(f"Instagram API error ({code}/{sub}): {msg}")
    return data


class Instagram:
    def __init__(self, user_id: str, token: str, version: str = "v25.0"):
        if not user_id or not token:
            raise IGError("Instagram not connected. Run: python -m reelagent setup-instagram")
        self.uid, self.token, self.ver = user_id, token, version
        self.base = f"https://graph.facebook.com/{version}"

    def account(self) -> dict:
        return _check(requests.get(f"{self.base}/{self.uid}", timeout=30, params={
            "fields": "username,followers_count,media_count", "access_token": self.token}))

    # ── reading Instagram (scout.py) ────────────────────────────────────────
    def hashtag_id(self, tag: str) -> str | None:
        """Hashtag Search. Counts toward Instagram's 30 unique hashtags per 7 days."""
        d = _check(requests.get(f"{self.base}/ig_hashtag_search", timeout=30, params={
            "user_id": self.uid, "q": tag.lstrip("#"), "access_token": self.token}))
        return (d.get("data") or [{}])[0].get("id")

    def hashtag_media(self, hashtag_id: str, edge: str = "top_media",
                      fields: str = "id,caption,like_count,comments_count,media_type,permalink,timestamp",
                      pages: int = 2) -> list[dict]:
        out, params = [], {"user_id": self.uid, "fields": fields, "limit": 50, "access_token": self.token}
        url = f"{self.base}/{hashtag_id}/{edge}"
        for _ in range(max(pages, 1)):
            d = _check(requests.get(url, params=params, timeout=30))
            out += d.get("data", [])
            after = (d.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not d.get("data"):
                break
            params["after"] = after
        return out

    def business_discovery_media(self, username: str, limit: int = 25) -> list[dict]:
        """Recent posts of another business/creator account (Business Discovery)."""
        f = (f"business_discovery.username({username}){{followers_count,media.limit({limit})"
             "{caption,like_count,comments_count,timestamp,media_type,media_product_type,permalink}}")
        d = _check(requests.get(f"{self.base}/{self.uid}", timeout=30, params={
            "fields": f, "access_token": self.token}))
        media = ((d.get("business_discovery") or {}).get("media") or {}).get("data", [])
        return [m for m in media if m.get("media_product_type") in (None, "REELS") or m.get("media_type") == "VIDEO"]

    def publishing_limit(self) -> dict:
        return _check(requests.get(f"{self.base}/{self.uid}/content_publishing_limit", timeout=30, params={
            "fields": "quota_usage,config", "access_token": self.token}))

    def publish_reel(self, video: Path, caption: str, share_to_feed: bool = True,
                     thumb_offset_ms: int | None = None, max_wait: int = 900,
                     audio: dict | None = None, trial: str | None = None) -> dict:
        """audio = {"audio_id", "audio_volume", "video_volume"} attaches an Instagram sound (Audio API).
        trial = "SS_PERFORMANCE" posts a trial reel: non-followers first, shared to followers if it performs."""
        video = Path(video)
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false", "access_token": self.token}
        if thumb_offset_ms is not None:
            data["thumb_offset"] = str(int(thumb_offset_ms))
        if audio:
            data["audio_configuration"] = json.dumps(audio)
        if trial:
            data["trial_params"] = json.dumps({"graduation_strategy": trial})
        c = _check(requests.post(f"{self.base}/{self.uid}/media", data=data, timeout=60))
        cid = c["id"]
        upload_uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.ver}/{cid}"

        size = video.stat().st_size
        with open(video, "rb") as f:
            _check(requests.post(upload_uri, data=f, timeout=1800, headers={
                "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)}))

        deadline = time.time() + max_wait
        while True:
            s = _check(requests.get(f"{self.base}/{cid}", timeout=30, params={
                "fields": "status_code,status", "access_token": self.token}))
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise IGFileRefused(f"Instagram could not process the video: {s.get('status', code)}")
            if code == "EXPIRED":
                raise IGError(f"Instagram could not process the video: {s.get('status', code)}")
            if time.time() > deadline:
                raise IGError("Timed out waiting for Instagram to process the video")
            time.sleep(6)

        pub = _check(requests.post(f"{self.base}/{self.uid}/media_publish", timeout=60, data={
            "creation_id": cid, "access_token": self.token}))
        media_id = pub["id"]
        link = ""
        try:
            link = _check(requests.get(f"{self.base}/{media_id}", timeout=30, params={
                "fields": "permalink", "access_token": self.token})).get("permalink", "")
        except IGError:
            pass
        return {"id": media_id, "permalink": link}


    # ── trends: Instagram's own sounds and audience timing ──────────────────
    def search_audio(self, query: str | None = None, audio_type: str = "music", limit: int = 25) -> list[dict]:
        """Instagram Audio API. No query = what's trending now. Only audio cleared for third-party use."""
        params = {"audio_type": audio_type, "user_id": self.uid, "access_token": self.token}
        if query:
            params["search_query"] = query
        items = _check(requests.get(f"{self.base}/ig_audio", params=params, timeout=30)).get("data", [])
        return items[:limit]

    def online_followers(self) -> dict[int, float]:
        """Average followers online per hour of day over the last 7 days ({} under 100 followers)."""
        now = int(time.time())
        d = _check(requests.get(f"{self.base}/{self.uid}/insights", timeout=30, params={
            "metric": "online_followers", "period": "lifetime", "since": now - 7 * 86400, "until": now,
            "access_token": self.token}))
        tot, cnt = {}, {}
        for m in d.get("data", []):
            for v in m.get("values", []):
                val = v.get("value")
                if isinstance(val, dict):
                    for h, c in val.items():
                        try:
                            hour = int(h)
                        except ValueError:
                            continue
                        tot[hour] = tot.get(hour, 0) + (c or 0)
                        cnt[hour] = cnt.get(hour, 0) + 1
        return {h: tot[h] / cnt[h] for h in tot}

    # ── engagement + analytics (ig-reply, ig-audit, ig-profile) ─────────────
    def profile(self) -> dict:
        return _check(requests.get(f"{self.base}/{self.uid}", timeout=30, params={
            "fields": "username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url",
            "access_token": self.token}))

    def recent_media(self, limit: int = 30) -> list[dict]:
        return _check(requests.get(f"{self.base}/{self.uid}/media", timeout=30, params={
            "fields": "id,caption,media_product_type,timestamp,permalink,like_count,comments_count",
            "limit": limit, "access_token": self.token})).get("data", [])

    def comments(self, media_id: str) -> list[dict]:
        return _check(requests.get(f"{self.base}/{media_id}/comments", timeout=30, params={
            "fields": "id,text,username,timestamp,like_count,replies{id,username,text}",
            "limit": 50, "access_token": self.token})).get("data", [])

    def reply(self, comment_id: str, message: str) -> str:
        return _check(requests.post(f"{self.base}/{comment_id}/replies", timeout=30, data={
            "message": message, "access_token": self.token})).get("id", "")

    def hide(self, comment_id: str) -> None:
        _check(requests.post(f"{self.base}/{comment_id}", timeout=30, data={
            "hide": "true", "access_token": self.token}))

    REEL_METRICS = ["views", "reach", "saved", "shares", "comments", "likes", "total_interactions",
                    "ig_reels_avg_watch_time"]

    def insights(self, media_id: str) -> dict:
        """Reel stats. Metrics Meta doesn't return for this media are simply left out."""
        def parse(data):
            out = {}
            for m in data.get("data", []):
                vals = m.get("values") or [{}]
                v = vals[0].get("value", (m.get("total_value") or {}).get("value"))
                if v is not None:
                    out[m["name"]] = v
            return out

        try:
            return parse(_check(requests.get(f"{self.base}/{media_id}/insights", timeout=30, params={
                "metric": ",".join(self.REEL_METRICS), "access_token": self.token})))
        except IGRateLimited:
            raise
        except IGError:
            out = {}
            for m in self.REEL_METRICS:          # one bad metric fails the batch: retry one by one
                try:
                    out.update(parse(_check(requests.get(f"{self.base}/{media_id}/insights", timeout=30,
                                                         params={"metric": m, "access_token": self.token}))))
                except IGRateLimited:
                    raise
                except IGError:
                    pass
            return out


def from_config(cfg) -> Instagram:
    return Instagram(cfg.ig_user_id, cfg.ig_token, cfg["instagram"].get("api_version", "v25.0"))


def linked_accounts(cfg, app_id: str, app_secret: str, short_token: str) -> list[dict]:
    """Exchange a Graph API Explorer token and list Pages with a linked Instagram professional account.
    Each item: {page, ig_id, username, token}. Page tokens minted from a long-lived user token don't expire."""
    base = f"https://graph.facebook.com/{cfg['instagram'].get('api_version', 'v25.0')}"
    ll = _check(requests.get(f"{base}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token", "client_id": app_id.strip(), "client_secret": app_secret.strip(),
        "fb_exchange_token": short_token.strip()}))["access_token"]
    pages = _check(requests.get(f"{base}/me/accounts", timeout=30, params={
        "fields": "name,access_token,instagram_business_account{id,username}", "access_token": ll})).get("data", [])
    out = [{"page": p["name"], "ig_id": p["instagram_business_account"]["id"],
            "username": p["instagram_business_account"].get("username", ""), "token": p["access_token"]}
           for p in pages if p.get("instagram_business_account")]
    if not out:
        raise IGError("None of your Facebook Pages has a linked Instagram professional account. "
                      "Switch Instagram to Creator/Business, link it to your Page, and try again.")
    return out


def save_account(cfg, acct: dict) -> dict:
    update_env({"IG_USER_ID": acct["ig_id"], "IG_ACCESS_TOKEN": acct["token"], "IG_USERNAME": acct["username"]})
    return Instagram(acct["ig_id"], acct["token"], cfg["instagram"].get("api_version", "v25.0")).account()


SETUP_STEPS = """One-time Instagram connection (~15 min):
  1. Instagram app → Settings → Account type → switch to Creator (or Business).
  2. Link the Instagram account to a Facebook Page you manage.
  3. developers.facebook.com → My Apps → Create App (type: Business).
  4. Tools → Graph API Explorer → pick your app → add permissions:
       instagram_basic, instagram_content_publish, instagram_manage_comments,
       instagram_manage_insights, pages_show_list, pages_read_engagement,
       business_management
     → Generate Access Token → copy it.
  5. App settings → Basic: copy the App ID and App Secret."""


def setup_interactive(cfg) -> None:
    print(SETUP_STEPS + "\n")
    app_id = input("App ID: ").strip()
    app_secret = getpass.getpass("App Secret (hidden): ").strip()
    short = getpass.getpass("Access token from Graph API Explorer (hidden): ").strip()
    accts = linked_accounts(cfg, app_id, app_secret, short)
    if len(accts) > 1:
        for i, a in enumerate(accts, 1):
            print(f"  {i}. {a['page']}  →  @{a['username']}")
        pick = accts[int(input("Which one? ").strip()) - 1]
    else:
        pick = accts[0]
    info = save_account(cfg, pick)
    print(f"\n✅ Connected @{info.get('username')} ({info.get('followers_count', '?')} followers). "
          "Saved to .env — keep that file private.")
