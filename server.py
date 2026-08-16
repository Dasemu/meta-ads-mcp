import base64
import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
META_DEFAULT_AD_ACCOUNT_ID = os.environ.get("META_DEFAULT_AD_ACCOUNT_ID")
GRAPH_HOST = f"https://graph.facebook.com/{META_API_VERSION}"
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("BIND_PORT", "8086"))

mcp = MCPServer("meta-ads")


def _act(ad_account_id: str | None) -> str:
    ad_account_id = ad_account_id or META_DEFAULT_AD_ACCOUNT_ID
    if not ad_account_id:
        raise ValueError("ad_account_id not given and no META_DEFAULT_AD_ACCOUNT_ID configured")
    return ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"


def _form(payload: dict) -> dict:
    """Graph API POST bodies are form-encoded; nested dicts/lists must be JSON strings."""
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in payload.items() if v is not None}


def _request(method: str, path: str, params: dict | None = None, **kwargs) -> httpx.Response:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["access_token"] = META_ACCESS_TOKEN
    with httpx.Client(timeout=60.0) as c:
        r = c.request(method, f"{GRAPH_HOST}{path}", params=params, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", r.text)
            except ValueError:
                detail = r.text
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {detail}")
        return r


def _paged(path: str, params: dict, limit: int) -> list[dict]:
    params = {**params, "limit": min(limit, 500)}
    out: list[dict] = []
    next_path, next_params = path, params
    while next_path and len(out) < limit:
        data = _request("GET", next_path, next_params).json()
        out.extend(data.get("data", []))
        paging = data.get("paging", {})
        cursors = paging.get("cursors", {})
        after = cursors.get("after")
        next_url = paging.get("next")
        if not next_url or not after:
            break
        next_path, next_params = path, {**params, "after": after}
    return out[:limit]


# ---------------------------------------------------------------------------
# Ad accounts
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ad_accounts() -> list[dict]:
    """List ad accounts (id, name, currency, status) reachable by this access token."""
    return _paged(
        "/me/adaccounts",
        {"fields": "id,name,account_status,currency,timezone_name,business_name"},
        limit=100,
    )


@mcp.tool()
def get_ad_account(ad_account_id: str | None = None) -> dict:
    """Get details for one ad account (id, name, currency, spend cap, status).
    Uses META_DEFAULT_AD_ACCOUNT_ID if ad_account_id is omitted."""
    return _request(
        "GET",
        f"/{_act(ad_account_id)}",
        {"fields": "id,name,account_status,currency,timezone_name,spend_cap,amount_spent,balance,business_name"},
    ).json()


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@mcp.tool()
def list_campaigns(ad_account_id: str | None = None, status_filter: str | None = None, limit: int = 50) -> list[dict]:
    """List campaigns in an ad account. status_filter is a comma-separated list of
    effective statuses (e.g. 'ACTIVE,PAUSED') or omit for all."""
    params = {"fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,special_ad_categories,created_time"}
    if status_filter:
        params["effective_status"] = f"[{','.join(f'\"{s.strip()}\"' for s in status_filter.split(','))}]"
    return _paged(f"/{_act(ad_account_id)}/campaigns", params, limit)


@mcp.tool()
def get_campaign(campaign_id: str) -> dict:
    """Get a single campaign's details."""
    return _request(
        "GET", f"/{campaign_id}",
        {"fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,bid_strategy,special_ad_categories,created_time,start_time,stop_time"},
    ).json()


@mcp.tool()
def create_campaign(
    name: str,
    objective: str,
    ad_account_id: str | None = None,
    status: str = "PAUSED",
    special_ad_categories: list[str] | None = None,
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_strategy: str | None = None,
) -> dict:
    """Create a campaign. objective is a Meta objective (e.g. 'OUTCOME_TRAFFIC',
    'OUTCOME_LEADS', 'OUTCOME_SALES', 'OUTCOME_AWARENESS', 'OUTCOME_ENGAGEMENT').
    special_ad_categories is required by Meta even if empty (e.g. [] or
    ['HOUSING','EMPLOYMENT','CREDIT','ISSUES_ELECTIONS_POLITICS']). Budgets are in
    the account's currency minor unit (e.g. cents). Defaults to status=PAUSED so
    nothing spends until explicitly activated."""
    payload = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories if special_ad_categories is not None else [],
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_strategy": bid_strategy,
    }
    return _request("POST", f"/{_act(ad_account_id)}/campaigns", data=_form(payload)).json()


@mcp.tool()
def update_campaign(
    campaign_id: str,
    name: str | None = None,
    status: str | None = None,
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_strategy: str | None = None,
) -> dict:
    """Update a campaign. Only the fields passed are changed. status accepts
    'ACTIVE', 'PAUSED', 'ARCHIVED' or 'DELETED' (Meta campaigns can't be hard
    deleted, only archived/deleted this way)."""
    payload = {
        "name": name,
        "status": status,
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_strategy": bid_strategy,
    }
    return _request("POST", f"/{campaign_id}", data=_form(payload)).json()


# ---------------------------------------------------------------------------
# Ad sets
# ---------------------------------------------------------------------------

@mcp.tool()
def list_adsets(ad_account_id: str | None = None, campaign_id: str | None = None, limit: int = 50) -> list[dict]:
    """List ad sets, either across an ad account or scoped to one campaign."""
    fields = "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,billing_event,optimization_goal,bid_amount,targeting"
    if campaign_id:
        return _paged(f"/{campaign_id}/adsets", {"fields": fields}, limit)
    return _paged(f"/{_act(ad_account_id)}/adsets", {"fields": fields}, limit)


@mcp.tool()
def get_adset(adset_id: str) -> dict:
    """Get a single ad set's details, including its targeting spec."""
    return _request(
        "GET", f"/{adset_id}",
        {"fields": "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,billing_event,optimization_goal,bid_amount,targeting,start_time,end_time"},
    ).json()


@mcp.tool()
def create_adset(
    campaign_id: str,
    name: str,
    targeting: dict,
    billing_event: str,
    optimization_goal: str,
    ad_account_id: str | None = None,
    status: str = "PAUSED",
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_amount: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    dsa_beneficiary: str | None = None,
    dsa_payor: str | None = None,
) -> dict:
    """Create an ad set under a campaign. targeting is a Meta targeting spec dict
    (e.g. {"geo_locations": {"countries": ["ES"]}, "age_min": 18, "age_max": 65}).
    billing_event (e.g. 'IMPRESSIONS') and optimization_goal (e.g. 'LINK_CLICKS',
    'LEAD_GENERATION') must be compatible with the campaign's objective. Exactly
    one of daily_budget/lifetime_budget is normally required unless the campaign
    uses campaign-level budget optimization. start_time/end_time are ISO 8601.
    dsa_beneficiary/dsa_payor are required by Meta for ads served in the EU
    (Digital Services Act disclosure). Defaults to status=PAUSED."""
    payload = {
        "name": name,
        "campaign_id": campaign_id,
        "targeting": targeting,
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "status": status,
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_amount": bid_amount,
        "start_time": start_time,
        "end_time": end_time,
        "dsa_beneficiary": dsa_beneficiary,
        "dsa_payor": dsa_payor,
    }
    return _request("POST", f"/{_act(ad_account_id)}/adsets", data=_form(payload)).json()


@mcp.tool()
def update_adset(
    adset_id: str,
    name: str | None = None,
    status: str | None = None,
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_amount: int | None = None,
    targeting: dict | None = None,
) -> dict:
    """Update an ad set. Only the fields passed are changed. status accepts
    'ACTIVE', 'PAUSED', 'ARCHIVED' or 'DELETED'."""
    payload = {
        "name": name,
        "status": status,
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_amount": bid_amount,
        "targeting": targeting,
    }
    return _request("POST", f"/{adset_id}", data=_form(payload)).json()


# ---------------------------------------------------------------------------
# Ad creatives
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ad_creatives(ad_account_id: str | None = None, limit: int = 50) -> list[dict]:
    """List ad creatives available in an ad account."""
    return _paged(
        f"/{_act(ad_account_id)}/adcreatives",
        {"fields": "id,name,object_story_spec,status,thumbnail_url"},
        limit,
    )


@mcp.tool()
def get_ad_creative(creative_id: str) -> dict:
    """Get a single ad creative's details."""
    return _request(
        "GET", f"/{creative_id}",
        {"fields": "id,name,object_story_spec,status,thumbnail_url,body,title,image_url"},
    ).json()


@mcp.tool()
def create_ad_creative(
    name: str,
    page_id: str,
    ad_account_id: str | None = None,
    message: str | None = None,
    link: str | None = None,
    image_hash: str | None = None,
    video_id: str | None = None,
    call_to_action_type: str | None = None,
    headline: str | None = None,
    description: str | None = None,
) -> dict:
    """Create an ad creative for a link/image or video ad, backed by a Facebook
    Page (page_id). Use upload_ad_image/upload_ad_video first to get image_hash/
    video_id. call_to_action_type is e.g. 'LEARN_MORE', 'SHOP_NOW', 'SIGN_UP'."""
    link_data: dict = {}
    if message is not None:
        link_data["message"] = message
    if link is not None:
        link_data["link"] = link
    if image_hash is not None:
        link_data["image_hash"] = image_hash
    if headline is not None:
        link_data["name"] = headline
    if description is not None:
        link_data["description"] = description
    if call_to_action_type is not None:
        link_data["call_to_action"] = {"type": call_to_action_type}

    object_story_spec: dict = {"page_id": page_id}
    if video_id is not None:
        video_data = {"video_id": video_id}
        if message is not None:
            video_data["message"] = message
        if call_to_action_type is not None:
            video_data["call_to_action"] = {"type": call_to_action_type}
        object_story_spec["video_data"] = video_data
    else:
        object_story_spec["link_data"] = link_data

    payload = {"name": name, "object_story_spec": object_story_spec}
    return _request("POST", f"/{_act(ad_account_id)}/adcreatives", data=_form(payload)).json()


@mcp.tool()
def delete_ad_creative(creative_id: str) -> str:
    """Delete an ad creative. Fails if it's still in use by an active ad."""
    _request("DELETE", f"/{creative_id}")
    return f"Deleted creative {creative_id}"


@mcp.tool()
def upload_ad_image(base64_content: str, filename: str, ad_account_id: str | None = None) -> dict:
    """Upload an image for use in ad creatives. Returns the image hash (and other
    metadata) to pass as image_hash to create_ad_creative."""
    content = base64.b64decode(base64_content)
    files = {filename: (filename, content)}
    return _request("POST", f"/{_act(ad_account_id)}/adimages", files=files).json()


@mcp.tool()
def upload_ad_video(base64_content: str, filename: str, ad_account_id: str | None = None,
                     title: str | None = None) -> dict:
    """Upload a video for use in ad creatives. Returns the video id to pass as
    video_id to create_ad_creative. Intended for small/medium video files (this
    does a single-request upload, not Meta's chunked resumable upload)."""
    content = base64.b64decode(base64_content)
    files = {"source": (filename, content)}
    data = {}
    if title is not None:
        data["title"] = title
    return _request("POST", f"/{_act(ad_account_id)}/advideos", data=data, files=files).json()


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ads(ad_account_id: str | None = None, adset_id: str | None = None, limit: int = 50) -> list[dict]:
    """List ads, either across an ad account or scoped to one ad set."""
    fields = "id,name,adset_id,campaign_id,status,effective_status,creative"
    if adset_id:
        return _paged(f"/{adset_id}/ads", {"fields": fields}, limit)
    return _paged(f"/{_act(ad_account_id)}/ads", {"fields": fields}, limit)


@mcp.tool()
def get_ad(ad_id: str) -> dict:
    """Get a single ad's details, including its creative id."""
    return _request(
        "GET", f"/{ad_id}",
        {"fields": "id,name,adset_id,campaign_id,status,effective_status,creative,tracking_specs"},
    ).json()


@mcp.tool()
def create_ad(adset_id: str, name: str, creative_id: str, ad_account_id: str | None = None,
               status: str = "PAUSED") -> dict:
    """Create an ad in an ad set, using an existing creative. Defaults to
    status=PAUSED so nothing goes live until explicitly activated."""
    payload = {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status,
    }
    return _request("POST", f"/{_act(ad_account_id)}/ads", data=_form(payload)).json()


@mcp.tool()
def update_ad(ad_id: str, name: str | None = None, status: str | None = None,
               creative_id: str | None = None) -> dict:
    """Update an ad. Only the fields passed are changed. status accepts 'ACTIVE',
    'PAUSED', 'ARCHIVED' or 'DELETED'."""
    payload = {
        "name": name,
        "status": status,
        "creative": {"creative_id": creative_id} if creative_id is not None else None,
    }
    return _request("POST", f"/{ad_id}", data=_form(payload)).json()


@mcp.tool()
def get_ad_preview(ad_id: str, ad_format: str = "DESKTOP_FEED_STANDARD") -> dict:
    """Get an HTML preview snippet for an ad. Common ad_format values:
    'DESKTOP_FEED_STANDARD', 'MOBILE_FEED_STANDARD', 'INSTAGRAM_STANDARD',
    'INSTAGRAM_STORY', 'FACEBOOK_STORY_MOBILE'."""
    return _request("GET", f"/{ad_id}/previews", {"ad_format": ad_format}).json()


# ---------------------------------------------------------------------------
# Insights (performance)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_insights(
    object_id: str,
    level: str = "campaign",
    date_preset: str = "last_7d",
    fields: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    breakdowns: list[str] | None = None,
    time_increment: str | None = None,
) -> list[dict]:
    """Get performance insights for a campaign/ad set/ad/ad account (object_id).
    level is 'account' | 'campaign' | 'adset' | 'ad'. date_preset is a Meta preset
    (e.g. 'today', 'yesterday', 'last_7d', 'last_30d', 'this_month') and is
    ignored if since/until (YYYY-MM-DD) are both given. fields defaults to a
    common set (impressions, clicks, spend, ctr, cpc, actions, reach) if omitted.
    time_increment='1' breaks the range down by day."""
    default_fields = ["impressions", "clicks", "spend", "ctr", "cpc", "reach", "actions", "cost_per_action_type"]
    params = {
        "level": level,
        "fields": ",".join(fields or default_fields),
    }
    if since and until:
        params["time_range"] = json.dumps({"since": since, "until": until})
    else:
        params["date_preset"] = date_preset
    if breakdowns:
        params["breakdowns"] = ",".join(breakdowns)
    if time_increment:
        params["time_increment"] = time_increment
    return _paged(f"/{object_id}/insights", params, limit=500)


# ---------------------------------------------------------------------------
# Custom audiences
# ---------------------------------------------------------------------------

@mcp.tool()
def list_custom_audiences(ad_account_id: str | None = None, limit: int = 50) -> list[dict]:
    """List custom audiences defined in an ad account."""
    return _paged(
        f"/{_act(ad_account_id)}/customaudiences",
        {"fields": "id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,operation_status"},
        limit,
    )


@mcp.tool()
def get_custom_audience(audience_id: str) -> dict:
    """Get a single custom audience's details."""
    return _request(
        "GET", f"/{audience_id}",
        {"fields": "id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,operation_status,description"},
    ).json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=BIND_HOST, port=BIND_PORT)
