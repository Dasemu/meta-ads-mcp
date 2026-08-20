import base64
import hashlib
import json
import os
import re
from urllib.parse import urlencode

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
    """Graph API POST/DELETE bodies are form-encoded; nested dicts/lists must be JSON strings."""
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in payload.items() if v is not None}


def _query_value(v):
    """Encode a query-param value the way Graph API expects: dicts and lists of
    dicts (time_range, filtering, targeting, ...) as JSON; lists of plain
    strings/numbers (fields, breakdowns, ...) as a comma-joined string."""
    if isinstance(v, dict):
        return json.dumps(v)
    if isinstance(v, list):
        if v and all(isinstance(i, dict) for i in v):
            return json.dumps(v)
        return ",".join(str(i) for i in v)
    return v


def _merge(named: dict, extra: dict | None) -> dict:
    """Combine explicit keyword args with a passthrough extra_params dict, so
    explicit args win when both set the same key but extra_params can supply
    anything not covered by the function's named parameters."""
    merged = dict(extra or {})
    merged.update({k: v for k, v in named.items() if v is not None})
    return merged


def _normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _request(method: str, path: str, params: dict | None = None, **kwargs) -> httpx.Response:
    params = {k: _query_value(v) for k, v in (params or {}).items() if v is not None}
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
# Custom audience PII normalization (Meta's documented hashing rules)
# ---------------------------------------------------------------------------

_PII_NORMALIZERS = {
    "EMAIL": lambda v: v.strip().lower(),
    "PHONE": lambda v: re.sub(r"[^0-9]", "", v),
    "GEN": lambda v: v.strip().lower()[:1],
    "DOBY": lambda v: re.sub(r"[^0-9]", "", v),
    "DOBM": lambda v: re.sub(r"[^0-9]", "", v).zfill(2),
    "DOBD": lambda v: re.sub(r"[^0-9]", "", v).zfill(2),
    "FN": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower(),
    "LN": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower(),
    "FI": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower()[:1],
    "CT": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower(),
    "ST": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower(),
    "ZIP": lambda v: v.strip().split("-")[0].lower(),
    "COUNTRY": lambda v: re.sub(r"[^a-zA-Z]", "", v).lower()[:2],
}
_PII_NO_HASH = {"EXTERN_ID", "MADID", "APPUID", "PAGEUID"}


def _hash_pii(value: str, field: str) -> str:
    normalize = _PII_NORMALIZERS.get(field.upper(), lambda v: v.strip().lower())
    return hashlib.sha256(normalize(str(value)).encode("utf-8")).hexdigest()


def _prepare_audience_row(row: list[str], schema: list[str]) -> list[str]:
    out = []
    for field, value in zip(schema, row):
        if value is None or field.upper() in _PII_NO_HASH:
            out.append(value)
        else:
            out.append(_hash_pii(value, field))
    return out


# ---------------------------------------------------------------------------
# Raw Graph API escape hatch
#
# The tools below this section wrap the most common ad-management calls with
# typed, documented parameters. They also all accept an extra_params (or
# params/fields) passthrough so nothing is truly capped. But the Marketing API
# is huge, so these four generic tools give the model full, direct access to
# ANY documented Graph API endpoint/parameter - the same power a developer
# reading developers.facebook.com/docs/marketing-api has with curl.
# ---------------------------------------------------------------------------

@mcp.tool()
def graph_get(path: str, params: dict | None = None, paginate: bool = False, limit: int = 100) -> dict | list:
    """Raw GET against the Marketing/Graph API - the full escape hatch for
    anything the other tools don't wrap explicitly. Use it for ANY endpoint or
    parameter documented at developers.facebook.com/docs/marketing-api. path is
    relative to the API version root, e.g. 'act_123456/campaigns', '123456'
    (a node id), or 'search' (root-level endpoints like targeting search).
    params are passed as query params: dicts and lists-of-dicts are
    JSON-encoded (matching how Graph API expects e.g. time_range/filtering/
    targeting); lists of plain strings/numbers are comma-joined (matching e.g.
    fields/breakdowns). If paginate=True, follows cursor pagination and returns
    a combined list up to limit; otherwise returns exactly what Meta returned
    (often {"data": [...], "paging": {...}})."""
    normalized = _normalize_path(path)
    if paginate:
        return _paged(normalized, params or {}, limit)
    return _request("GET", normalized, params).json()


@mcp.tool()
def graph_post(path: str, params: dict | None = None) -> dict:
    """Raw POST against the Marketing/Graph API - create or update ANY node or
    edge with ANY parameters, exactly as documented at
    developers.facebook.com/docs/marketing-api. Use this for anything the
    dedicated create_*/update_* tools don't expose. path is relative to the API
    version root (e.g. 'act_123456/campaigns' to create under an ad account, or
    '123456789' to update a node in place). Dict/list values in params are
    JSON-encoded automatically, matching what Graph API expects for structured
    fields (targeting, promoted_object, special_ad_categories, ...)."""
    return _request("POST", _normalize_path(path), data=_form(params or {})).json()


@mcp.tool()
def graph_delete(path: str, params: dict | None = None) -> dict:
    """Raw DELETE against the Marketing/Graph API. path is relative to the API
    version root - usually a node id (deletes that object) or an edge used for
    association-style deletes (e.g. removing custom audience members via
    '<audience_id>/users'). Dict/list values in params are JSON-encoded
    automatically."""
    return _request("DELETE", _normalize_path(path), data=_form(params) if params else None).json()


@mcp.tool()
def graph_batch(requests: list[dict]) -> list[dict]:
    """Execute up to 50 Graph API calls in a single HTTP round trip (Meta's
    batch API) - useful for reading or writing many objects efficiently. Each
    item in requests is {"method": "GET"|"POST"|"DELETE", "relative_url":
    "act_123/campaigns?fields=id,name", "params": {...}}. params is optional
    and merged in: for GET it's appended to relative_url's query string; for
    POST/DELETE it's form-encoded into the sub-request body (dicts/lists
    JSON-encoded). Returns one {"code": int, "body": <parsed JSON or raw
    text>} per request, in the same order as requests."""
    batch = []
    for req in requests:
        method = req.get("method", "GET").upper()
        relative_url = req["relative_url"].lstrip("/")
        extra = req.get("params") or {}
        item = {"method": method}
        if method == "GET":
            if extra:
                encoded = urlencode({k: _query_value(v) for k, v in extra.items() if v is not None})
                sep = "&" if "?" in relative_url else "?"
                relative_url = f"{relative_url}{sep}{encoded}"
            item["relative_url"] = relative_url
        else:
            item["relative_url"] = relative_url
            if extra:
                item["body"] = urlencode(_form(extra))
        batch.append(item)
    responses = _request("POST", "/", data={"batch": json.dumps(batch)}).json()
    out = []
    for r in responses or []:
        body = r.get("body") if isinstance(r, dict) else None
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            parsed = body
        out.append({"code": r.get("code") if isinstance(r, dict) else None, "body": parsed})
    return out


# ---------------------------------------------------------------------------
# Ad accounts
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ad_accounts(fields: list[str] | None = None) -> list[dict]:
    """List ad accounts (id, name, currency, status by default) reachable by
    this access token. Pass fields to request a different/larger field set."""
    default_fields = "id,name,account_status,currency,timezone_name,business_name"
    return _paged("/me/adaccounts", {"fields": ",".join(fields) if fields else default_fields}, limit=100)


@mcp.tool()
def get_ad_account(ad_account_id: str | None = None, fields: list[str] | None = None) -> dict:
    """Get details for one ad account. Uses META_DEFAULT_AD_ACCOUNT_ID if
    ad_account_id is omitted. Pass fields to request a different/larger field
    set (e.g. add 'funding_source_details', 'min_daily_budget', 'disable_reason')."""
    default_fields = "id,name,account_status,currency,timezone_name,spend_cap,amount_spent,balance,business_name"
    return _request(
        "GET", f"/{_act(ad_account_id)}",
        {"fields": ",".join(fields) if fields else default_fields},
    ).json()


@mcp.tool()
def list_ad_account_users(ad_account_id: str | None = None) -> list[dict]:
    """List people with access to an ad account, and their role/tasks."""
    return _request("GET", f"/{_act(ad_account_id)}/users", {"fields": "id,name,role,tasks"}).json().get("data", [])


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@mcp.tool()
def list_campaigns(
    ad_account_id: str | None = None,
    status_filter: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    extra_params: dict | None = None,
) -> list[dict]:
    """List campaigns in an ad account. status_filter is a comma-separated list
    of effective statuses (e.g. 'ACTIVE,PAUSED') or omit for all. Pass fields to
    request a different/larger field set; extra_params for anything else (e.g.
    filtering=[{"field":"name","operator":"CONTAIN","value":"summer"}])."""
    default_fields = "id,name,objective,status,effective_status,daily_budget,lifetime_budget,special_ad_categories,buying_type,created_time"
    params = {"fields": ",".join(fields) if fields else default_fields}
    if status_filter:
        params["effective_status"] = json.dumps([s.strip() for s in status_filter.split(",")])
    return _paged(f"/{_act(ad_account_id)}/campaigns", _merge(params, extra_params), limit)


@mcp.tool()
def get_campaign(campaign_id: str, fields: list[str] | None = None) -> dict:
    """Get a single campaign's details. Pass fields to request a
    different/larger field set."""
    default_fields = (
        "id,name,objective,status,effective_status,daily_budget,lifetime_budget,bid_strategy,"
        "buying_type,spend_cap,special_ad_categories,special_ad_category_country,created_time,start_time,stop_time"
    )
    return _request("GET", f"/{campaign_id}", {"fields": ",".join(fields) if fields else default_fields}).json()


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
    buying_type: str | None = None,
    spend_cap: int | None = None,
    special_ad_category_country: list[str] | None = None,
    adlabels: list[dict] | None = None,
    start_time: str | None = None,
    stop_time: str | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create a campaign. objective is a Meta objective (e.g. 'OUTCOME_TRAFFIC',
    'OUTCOME_LEADS', 'OUTCOME_SALES', 'OUTCOME_AWARENESS', 'OUTCOME_ENGAGEMENT').
    special_ad_categories is required by Meta even if empty (e.g. [] or
    ['HOUSING','EMPLOYMENT','CREDIT','ISSUES_ELECTIONS_POLITICS']); when set,
    special_ad_category_country (list of ISO country codes) is often required
    too. Budgets are in the account's currency minor unit (e.g. cents).
    buying_type is 'AUCTION' (default) or 'RESERVED' (reach & frequency).
    spend_cap is a hard lifetime cap in minor currency unit. adlabels is e.g.
    [{"id": "<label_id>"}] (see list_ad_labels/create_ad_label). Defaults to
    status=PAUSED so nothing spends until explicitly activated. extra_params
    passes through any other Marketing API field for /campaigns not listed
    above (e.g. a campaign-level 'promoted_object' for some objectives);
    explicit parameters above take precedence if both set the same key."""
    payload = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories if special_ad_categories is not None else [],
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_strategy": bid_strategy,
        "buying_type": buying_type,
        "spend_cap": spend_cap,
        "special_ad_category_country": special_ad_category_country,
        "adlabels": adlabels,
        "start_time": start_time,
        "stop_time": stop_time,
    }
    return _request("POST", f"/{_act(ad_account_id)}/campaigns", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def update_campaign(
    campaign_id: str,
    name: str | None = None,
    status: str | None = None,
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_strategy: str | None = None,
    spend_cap: int | None = None,
    adlabels: list[dict] | None = None,
    stop_time: str | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Update a campaign. Only the fields passed are changed. status accepts
    'ACTIVE', 'PAUSED', 'ARCHIVED' or 'DELETED'. extra_params passes through any
    other Marketing API field for this node."""
    payload = {
        "name": name,
        "status": status,
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_strategy": bid_strategy,
        "spend_cap": spend_cap,
        "adlabels": adlabels,
        "stop_time": stop_time,
    }
    return _request("POST", f"/{campaign_id}", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def delete_campaign(campaign_id: str) -> str:
    """Permanently delete a campaign (and, per Meta's cascade rules, its ad
    sets/ads). Prefer update_campaign(status='PAUSED'/'ARCHIVED') if you might
    want it back - deletion cannot be undone through the API."""
    _request("DELETE", f"/{campaign_id}")
    return f"Deleted campaign {campaign_id}"


@mcp.tool()
def duplicate_campaign(
    campaign_id: str,
    deep_copy: bool = True,
    status_option: str = "PAUSED",
    rename_suffix: str | None = " - Copy",
    extra_params: dict | None = None,
) -> dict:
    """Duplicate a campaign (the same as Ads Manager's 'Duplicate' feature).
    deep_copy=True also duplicates its child ad sets/ads/creatives; False
    duplicates only the campaign itself. status_option is 'ACTIVE', 'PAUSED',
    or 'INHERITED_FROM_SOURCE'. Returns the new object ids, e.g.
    {"copied_campaign_id": ..., "ad_object_ids": [...]}."""
    payload = {"deep_copy": deep_copy, "status_option": status_option}
    if rename_suffix is not None:
        payload["rename_options"] = {"rename_strategy": "ONLY_TOP_LEVEL_RENAME", "rename_suffix": rename_suffix}
    return _request("POST", f"/{campaign_id}/copies", data=_form(_merge(payload, extra_params))).json()


# ---------------------------------------------------------------------------
# Ad sets
# ---------------------------------------------------------------------------

@mcp.tool()
def list_adsets(
    ad_account_id: str | None = None,
    campaign_id: str | None = None,
    status_filter: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    extra_params: dict | None = None,
) -> list[dict]:
    """List ad sets, either across an ad account or scoped to one campaign.
    status_filter is a comma-separated list of effective statuses (e.g.
    'ACTIVE,PAUSED')."""
    default_fields = (
        "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,billing_event,"
        "optimization_goal,bid_amount,bid_strategy,targeting,promoted_object,start_time,end_time"
    )
    params = {"fields": ",".join(fields) if fields else default_fields}
    if status_filter:
        params["effective_status"] = json.dumps([s.strip() for s in status_filter.split(",")])
    params = _merge(params, extra_params)
    path = f"/{campaign_id}/adsets" if campaign_id else f"/{_act(ad_account_id)}/adsets"
    return _paged(path, params, limit)


@mcp.tool()
def get_adset(adset_id: str, fields: list[str] | None = None) -> dict:
    """Get a single ad set's details, including its targeting spec."""
    default_fields = (
        "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,billing_event,"
        "optimization_goal,bid_amount,bid_strategy,targeting,promoted_object,attribution_spec,"
        "frequency_control_specs,pacing_type,adset_schedule,destination_type,is_dynamic_creative,start_time,end_time"
    )
    return _request("GET", f"/{adset_id}", {"fields": ",".join(fields) if fields else default_fields}).json()


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
    bid_strategy: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    dsa_beneficiary: str | None = None,
    dsa_payor: str | None = None,
    promoted_object: dict | None = None,
    attribution_spec: list[dict] | None = None,
    frequency_control_specs: list[dict] | None = None,
    pacing_type: list[str] | None = None,
    adset_schedule: list[dict] | None = None,
    destination_type: str | None = None,
    is_dynamic_creative: bool | None = None,
    adlabels: list[dict] | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create an ad set under a campaign. targeting is a Meta targeting spec dict
    (e.g. {"geo_locations": {"countries": ["ES"]}, "age_min": 18, "age_max": 65}).
    billing_event (e.g. 'IMPRESSIONS') and optimization_goal (e.g. 'LINK_CLICKS',
    'LEAD_GENERATION') must be compatible with the campaign's objective. Exactly
    one of daily_budget/lifetime_budget is normally required unless the campaign
    uses campaign-level budget optimization. start_time/end_time are ISO 8601.
    dsa_beneficiary/dsa_payor are required by Meta for ads served in the EU
    (Digital Services Act disclosure). promoted_object ties the ad set to a
    pixel/app/page depending on objective, e.g. {"pixel_id": "...",
    "custom_event_type": "PURCHASE"} for OUTCOME_SALES conversions, {"page_id":
    "..."} for Page-object objectives, or {"application_id": ...,
    "object_store_url": "..."} for app installs. attribution_spec is e.g.
    [{"event_type": "CLICK_THROUGH", "window_days": 7}]. frequency_control_specs
    caps delivery, e.g. [{"event": "IMPRESSIONS", "interval_days": 7,
    "max_frequency": 2}]. adset_schedule is a dayparting list of {"days":
    [0,1,2,3,4,5,6], "start_minute": 0, "end_minute": 1440, "timezone_type":
    "USER"}. destination_type is e.g. 'WEBSITE','APP','MESSENGER','WHATSAPP'.
    Defaults to status=PAUSED. extra_params passes through any other Marketing
    API field for /adsets not listed above; explicit parameters above take
    precedence if both set the same key."""
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
        "bid_strategy": bid_strategy,
        "start_time": start_time,
        "end_time": end_time,
        "dsa_beneficiary": dsa_beneficiary,
        "dsa_payor": dsa_payor,
        "promoted_object": promoted_object,
        "attribution_spec": attribution_spec,
        "frequency_control_specs": frequency_control_specs,
        "pacing_type": pacing_type,
        "adset_schedule": adset_schedule,
        "destination_type": destination_type,
        "is_dynamic_creative": is_dynamic_creative,
        "adlabels": adlabels,
    }
    return _request("POST", f"/{_act(ad_account_id)}/adsets", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def update_adset(
    adset_id: str,
    name: str | None = None,
    status: str | None = None,
    daily_budget: int | None = None,
    lifetime_budget: int | None = None,
    bid_amount: int | None = None,
    bid_strategy: str | None = None,
    targeting: dict | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    frequency_control_specs: list[dict] | None = None,
    pacing_type: list[str] | None = None,
    adset_schedule: list[dict] | None = None,
    is_dynamic_creative: bool | None = None,
    adlabels: list[dict] | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Update an ad set. Only the fields passed are changed. status accepts
    'ACTIVE', 'PAUSED', 'ARCHIVED' or 'DELETED'. extra_params passes through any
    other Marketing API field for this node (e.g. 'dsa_beneficiary'/
    'dsa_payor', though Meta may reject changes to some fields post-creation)."""
    payload = {
        "name": name,
        "status": status,
        "daily_budget": daily_budget,
        "lifetime_budget": lifetime_budget,
        "bid_amount": bid_amount,
        "bid_strategy": bid_strategy,
        "targeting": targeting,
        "start_time": start_time,
        "end_time": end_time,
        "frequency_control_specs": frequency_control_specs,
        "pacing_type": pacing_type,
        "adset_schedule": adset_schedule,
        "is_dynamic_creative": is_dynamic_creative,
        "adlabels": adlabels,
    }
    return _request("POST", f"/{adset_id}", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def delete_adset(adset_id: str) -> str:
    """Permanently delete an ad set (and its ads). Prefer
    update_adset(status='PAUSED'/'ARCHIVED') if you might want it back."""
    _request("DELETE", f"/{adset_id}")
    return f"Deleted ad set {adset_id}"


@mcp.tool()
def duplicate_adset(
    adset_id: str,
    deep_copy: bool = True,
    status_option: str = "PAUSED",
    rename_suffix: str | None = " - Copy",
    campaign_id: str | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Duplicate an ad set (optionally its ads too), optionally into a
    different campaign_id. See duplicate_campaign for status_option/deep_copy
    semantics."""
    payload = {"deep_copy": deep_copy, "status_option": status_option, "campaign_id": campaign_id}
    if rename_suffix is not None:
        payload["rename_options"] = {"rename_strategy": "ONLY_TOP_LEVEL_RENAME", "rename_suffix": rename_suffix}
    return _request("POST", f"/{adset_id}/copies", data=_form(_merge(payload, extra_params))).json()


# ---------------------------------------------------------------------------
# Ad creatives
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ad_creatives(ad_account_id: str | None = None, fields: list[str] | None = None, limit: int = 50) -> list[dict]:
    """List ad creatives available in an ad account."""
    default_fields = "id,name,object_story_spec,asset_feed_spec,status,thumbnail_url"
    return _paged(f"/{_act(ad_account_id)}/adcreatives", {"fields": ",".join(fields) if fields else default_fields}, limit)


@mcp.tool()
def get_ad_creative(creative_id: str, fields: list[str] | None = None) -> dict:
    """Get a single ad creative's details."""
    default_fields = (
        "id,name,object_story_spec,asset_feed_spec,status,thumbnail_url,body,title,image_url,"
        "instagram_actor_id,url_tags,degrees_of_freedom_spec"
    )
    return _request("GET", f"/{creative_id}", {"fields": ",".join(fields) if fields else default_fields}).json()


@mcp.tool()
def create_ad_creative(
    name: str,
    page_id: str | None = None,
    ad_account_id: str | None = None,
    message: str | None = None,
    link: str | None = None,
    image_hash: str | None = None,
    video_id: str | None = None,
    call_to_action_type: str | None = None,
    headline: str | None = None,
    description: str | None = None,
    object_story_id: str | None = None,
    instagram_user_id: str | None = None,
    source_instagram_media_id: str | None = None,
    child_attachments: list[dict] | None = None,
    object_story_spec: dict | None = None,
    asset_feed_spec: dict | None = None,
    degrees_of_freedom_spec: dict | None = None,
    url_tags: str | None = None,
    instagram_actor_id: str | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create an ad creative. For the common case (link/image/video ad backed by
    a Facebook Page), pass page_id plus message/link/image_hash/video_id/
    headline/description/call_to_action_type; use upload_ad_image/
    upload_ad_video first to get image_hash/video_id. call_to_action_type is
    e.g. 'LEARN_MORE', 'SHOP_NOW', 'SIGN_UP'.

    For a carousel ad, also pass child_attachments - a list of {"link": ...,
    "name": ..., "description": ..., "image_hash" or "video_id": ...,
    "call_to_action": {"type": "..."}} dicts, one per card.

    To instead promote an existing organic post as-is, pass one of:
    - object_story_id: an existing Facebook Page post, as "<page_id>_<post_id>".
    - instagram_user_id + source_instagram_media_id: an existing Instagram post
      (source_instagram_media_id is the IG media id); page_id must still be set
      to the Facebook Page linked to that Instagram account.
    In both cases message/link/image_hash/video_id/headline/description/
    call_to_action_type/child_attachments are ignored since the ad reuses the
    published post.

    For full control (collection ads, Advantage+ / dynamic catalog ads,
    anything not covered above), pass object_story_spec and/or asset_feed_spec
    directly - when object_story_spec is given explicitly it's sent as-is
    instead of being assembled from the fields above. asset_feed_spec drives
    dynamic/Advantage+ creative (multiple text/image/video assets with
    placement customization rules). degrees_of_freedom_spec controls
    Advantage+ creative enhancements (e.g. turning standard enhancements on/
    off). url_tags is a query string appended to all destination links (e.g.
    'utm_source=facebook&utm_medium=paid'). instagram_actor_id picks which
    connected Instagram account the ad is shown as coming from. extra_params
    passes through any other Marketing API field for /adcreatives; explicit
    parameters above take precedence if both set the same key."""
    if object_story_spec is not None:
        payload = {"name": name, "object_story_spec": object_story_spec}
    elif object_story_id is not None:
        payload = {"name": name, "object_story_id": object_story_id}
    elif source_instagram_media_id is not None:
        payload = {
            "name": name,
            "object_id": page_id,
            "instagram_user_id": instagram_user_id,
            "source_instagram_media_id": source_instagram_media_id,
        }
    else:
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
        if child_attachments is not None:
            link_data["child_attachments"] = child_attachments

        story_spec: dict = {"page_id": page_id}
        if video_id is not None:
            video_data = {"video_id": video_id}
            if message is not None:
                video_data["message"] = message
            if call_to_action_type is not None:
                video_data["call_to_action"] = {"type": call_to_action_type}
            story_spec["video_data"] = video_data
        else:
            story_spec["link_data"] = link_data

        payload = {"name": name, "object_story_spec": story_spec}

    payload["asset_feed_spec"] = asset_feed_spec
    payload["degrees_of_freedom_spec"] = degrees_of_freedom_spec
    payload["url_tags"] = url_tags
    payload["instagram_actor_id"] = instagram_actor_id

    return _request("POST", f"/{_act(ad_account_id)}/adcreatives", data=_form(_merge(payload, extra_params))).json()


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
def list_ads(
    ad_account_id: str | None = None,
    adset_id: str | None = None,
    status_filter: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    extra_params: dict | None = None,
) -> list[dict]:
    """List ads, either across an ad account or scoped to one ad set.
    status_filter is a comma-separated list of effective statuses (e.g.
    'ACTIVE,PAUSED')."""
    default_fields = "id,name,adset_id,campaign_id,status,effective_status,creative,tracking_specs,conversion_domain"
    params = {"fields": ",".join(fields) if fields else default_fields}
    if status_filter:
        params["effective_status"] = json.dumps([s.strip() for s in status_filter.split(",")])
    params = _merge(params, extra_params)
    path = f"/{adset_id}/ads" if adset_id else f"/{_act(ad_account_id)}/ads"
    return _paged(path, params, limit)


@mcp.tool()
def get_ad(ad_id: str, fields: list[str] | None = None) -> dict:
    """Get a single ad's details, including its creative id."""
    default_fields = "id,name,adset_id,campaign_id,status,effective_status,creative,tracking_specs,conversion_domain,adlabels"
    return _request("GET", f"/{ad_id}", {"fields": ",".join(fields) if fields else default_fields}).json()


@mcp.tool()
def create_ad(
    adset_id: str,
    name: str,
    creative_id: str,
    ad_account_id: str | None = None,
    status: str = "PAUSED",
    tracking_specs: list[dict] | None = None,
    conversion_domain: str | None = None,
    adlabels: list[dict] | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create an ad in an ad set, using an existing creative. Defaults to
    status=PAUSED so nothing goes live until explicitly activated.
    conversion_domain is required by Meta for some objectives to verify domain
    ownership for attribution. extra_params passes through any other Marketing
    API field for /ads; explicit parameters above take precedence if both set
    the same key."""
    payload = {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status,
        "tracking_specs": tracking_specs,
        "conversion_domain": conversion_domain,
        "adlabels": adlabels,
    }
    return _request("POST", f"/{_act(ad_account_id)}/ads", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def update_ad(
    ad_id: str,
    name: str | None = None,
    status: str | None = None,
    creative_id: str | None = None,
    tracking_specs: list[dict] | None = None,
    conversion_domain: str | None = None,
    adlabels: list[dict] | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Update an ad. Only the fields passed are changed. status accepts 'ACTIVE',
    'PAUSED', 'ARCHIVED' or 'DELETED'. extra_params passes through any other
    Marketing API field for this node."""
    payload = {
        "name": name,
        "status": status,
        "creative": {"creative_id": creative_id} if creative_id is not None else None,
        "tracking_specs": tracking_specs,
        "conversion_domain": conversion_domain,
        "adlabels": adlabels,
    }
    return _request("POST", f"/{ad_id}", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def delete_ad(ad_id: str) -> str:
    """Permanently delete an ad. Prefer update_ad(status='PAUSED'/'ARCHIVED') if
    you might want it back."""
    _request("DELETE", f"/{ad_id}")
    return f"Deleted ad {ad_id}"


@mcp.tool()
def get_ad_preview(ad_id: str, ad_format: str = "DESKTOP_FEED_STANDARD", extra_params: dict | None = None) -> dict:
    """Get an HTML preview snippet for an ad. Common ad_format values:
    'DESKTOP_FEED_STANDARD', 'MOBILE_FEED_STANDARD', 'INSTAGRAM_STANDARD',
    'INSTAGRAM_STORY', 'FACEBOOK_STORY_MOBILE'. extra_params can add e.g.
    'creative_feature' or 'product_item_ids' for catalog ads."""
    return _request("GET", f"/{ad_id}/previews", _merge({"ad_format": ad_format}, extra_params)).json()


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
    action_breakdowns: list[str] | None = None,
    time_increment: str | None = None,
    filtering: list[dict] | None = None,
    sort: list[str] | None = None,
    limit: int = 500,
    extra_params: dict | None = None,
) -> list[dict]:
    """Get performance insights for a campaign/ad set/ad/ad account (object_id).
    level is 'account' | 'campaign' | 'adset' | 'ad'. date_preset is a Meta preset
    (e.g. 'today', 'yesterday', 'last_7d', 'last_30d', 'this_month') and is
    ignored if since/until (YYYY-MM-DD) are both given. fields defaults to a
    common set (impressions, clicks, spend, ctr, cpc, actions, reach) if omitted.
    time_increment='1' breaks the range down by day. filtering is a list of
    {"field": ..., "operator": ..., "value": ...} dicts. sort is a list of
    field names, each optionally suffixed '_ascending'/'_descending' (e.g.
    ['spend_descending']). For very large exports that risk timing out, use
    create_insights_report instead. extra_params passes through any other
    Marketing API insights parameter (e.g. 'action_attribution_windows',
    'use_unified_attribution_setting')."""
    default_fields = ["impressions", "clicks", "spend", "ctr", "cpc", "reach", "actions", "cost_per_action_type"]
    params = {
        "level": level,
        "fields": ",".join(fields or default_fields),
    }
    if since and until:
        params["time_range"] = {"since": since, "until": until}
    else:
        params["date_preset"] = date_preset
    if breakdowns:
        params["breakdowns"] = breakdowns
    if action_breakdowns:
        params["action_breakdowns"] = action_breakdowns
    if time_increment:
        params["time_increment"] = time_increment
    if filtering:
        params["filtering"] = filtering
    if sort:
        params["sort"] = sort
    return _paged(f"/{object_id}/insights", _merge(params, extra_params), limit)


@mcp.tool()
def create_insights_report(
    object_id: str,
    level: str = "campaign",
    date_preset: str = "last_30d",
    fields: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: str | None = None,
    filtering: list[dict] | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Start an asynchronous insights report for large exports (Meta processes
    these in the background instead of the request risking a timeout). Returns
    {"report_run_id": ...} - poll with get_insights_report_status, then fetch
    rows with get_insights_report_results once async_status is 'Job Completed'.
    Other parameters match get_insights."""
    default_fields = ["impressions", "clicks", "spend", "ctr", "cpc", "reach", "actions", "cost_per_action_type"]
    params = {
        "level": level,
        "fields": ",".join(fields or default_fields),
    }
    if since and until:
        params["time_range"] = {"since": since, "until": until}
    else:
        params["date_preset"] = date_preset
    if breakdowns:
        params["breakdowns"] = breakdowns
    if action_breakdowns:
        params["action_breakdowns"] = action_breakdowns
    if time_increment:
        params["time_increment"] = time_increment
    if filtering:
        params["filtering"] = filtering
    return _request("POST", f"/{object_id}/insights", data=_form(_merge(params, extra_params))).json()


@mcp.tool()
def get_insights_report_status(report_run_id: str) -> dict:
    """Check the status of an async insights report (see
    create_insights_report). async_status becomes 'Job Completed' when
    async_percent_completion reaches 100."""
    return _request("GET", f"/{report_run_id}", {"fields": "id,async_status,async_percent_completion"}).json()


@mcp.tool()
def get_insights_report_results(report_run_id: str, limit: int = 500) -> list[dict]:
    """Fetch the result rows of a completed async insights report (see
    create_insights_report / get_insights_report_status)."""
    return _paged(f"/{report_run_id}/insights", {}, limit)


# ---------------------------------------------------------------------------
# Custom audiences
# ---------------------------------------------------------------------------

@mcp.tool()
def list_custom_audiences(ad_account_id: str | None = None, fields: list[str] | None = None, limit: int = 50) -> list[dict]:
    """List custom audiences defined in an ad account."""
    default_fields = "id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,operation_status"
    return _paged(f"/{_act(ad_account_id)}/customaudiences", {"fields": ",".join(fields) if fields else default_fields}, limit)


@mcp.tool()
def get_custom_audience(audience_id: str, fields: list[str] | None = None) -> dict:
    """Get a single custom audience's details."""
    default_fields = (
        "id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,"
        "operation_status,description,rule,lookalike_spec"
    )
    return _request("GET", f"/{audience_id}", {"fields": ",".join(fields) if fields else default_fields}).json()


@mcp.tool()
def create_custom_audience(
    name: str,
    subtype: str,
    ad_account_id: str | None = None,
    description: str | None = None,
    customer_file_source: str | None = None,
    pixel_id: str | None = None,
    rule: dict | None = None,
    retention_days: int | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create a custom audience. subtype is e.g. 'CUSTOM' (a customer list - then
    add members with add_users_to_custom_audience), 'WEBSITE' (pixel-based rule
    audience - set pixel_id/rule), 'ENGAGEMENT' (people who engaged with your
    Page/Instagram/ads/events - set rule), 'APP' (app activity - set rule).
    customer_file_source is required for CUSTOM (e.g. 'USER_PROVIDED_ONLY',
    'PARTNER_PROVIDED_ONLY', 'BOTH_USER_AND_PARTNER_PROVIDED'). rule is a JSON
    filter dict for WEBSITE/ENGAGEMENT/APP subtypes, e.g. {"inclusions":
    {"operator": "or", "rules": [{"event_sources": [{"id": pixel_id, "type":
    "pixel"}], "retention_seconds": 2592000, "filter": {"operator": "and",
    "filters": [{"field": "event", "operator": "=", "value": "Purchase"}]}}]}}.
    For a lookalike audience use create_lookalike_audience instead. extra_params
    passes through any other Marketing API field for /customaudiences."""
    payload = {
        "name": name,
        "subtype": subtype,
        "description": description,
        "customer_file_source": customer_file_source,
        "pixel_id": pixel_id,
        "rule": rule,
        "retention_days": retention_days,
    }
    return _request("POST", f"/{_act(ad_account_id)}/customaudiences", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def create_lookalike_audience(
    name: str,
    origin_audience_id: str,
    country: str | None = None,
    ratio: float | None = None,
    ad_account_id: str | None = None,
    lookalike_spec: dict | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create a lookalike audience modeled on an existing custom audience
    (origin_audience_id needs at least ~100 members/conversions, 200+
    recommended). country is an ISO country code (e.g. 'ES'). ratio is the
    target audience size as a fraction of that country's population (e.g. 0.01
    for the closest-matching 1%; Meta typically allows roughly 0.01-0.20). Pass
    lookalike_spec directly for full control instead (e.g. a starting_ratio/
    ratio range, or multiple target_countries) - it then overrides
    country/ratio entirely."""
    spec = lookalike_spec or {k: v for k, v in {"type": "similarity", "country": country, "ratio": ratio}.items() if v is not None}
    payload = {
        "name": name,
        "subtype": "LOOKALIKE",
        "origin_audience_id": origin_audience_id,
        "lookalike_spec": spec,
    }
    return _request("POST", f"/{_act(ad_account_id)}/customaudiences", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def update_custom_audience(
    audience_id: str,
    name: str | None = None,
    description: str | None = None,
    rule: dict | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Update a custom audience's name/description/rule (rule only applies to
    WEBSITE/ENGAGEMENT/APP subtypes). extra_params passes through any other
    Marketing API field for this node."""
    payload = {"name": name, "description": description, "rule": rule}
    return _request("POST", f"/{audience_id}", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def delete_custom_audience(audience_id: str) -> str:
    """Permanently delete a custom audience."""
    _request("DELETE", f"/{audience_id}")
    return f"Deleted custom audience {audience_id}"


@mcp.tool()
def add_users_to_custom_audience(
    audience_id: str,
    schema: list[str],
    users: list[list[str]],
    pre_hashed: bool = False,
) -> dict:
    """Add people to a CUSTOM (customer list) audience for matching. schema is
    the list of field keys per column, e.g. ["EMAIL", "PHONE"]. users is a list
    of rows with one value per schema column, e.g.
    [["jane@example.com", "+34123456789"]]. By default (pre_hashed=False) each
    value is normalized per Meta's rules (lowercased/trimmed email, digits-only
    phone, letters-only name/city/state, zero-padded month/day, etc.) and
    SHA256-hashed before sending - EXTERN_ID and MADID are sent as-is, never
    hashed, since Meta doesn't treat them as raw PII. Set pre_hashed=True if you
    already normalized and SHA256-hashed the values yourself. Recognized schema
    keys: EMAIL, PHONE, GEN, DOBY, DOBM, DOBD, FN, LN, FI, CT, ST, ZIP, COUNTRY,
    MADID, EXTERN_ID."""
    data = users if pre_hashed else [_prepare_audience_row(row, schema) for row in users]
    payload = {"payload": {"schema": schema, "data": data}}
    return _request("POST", f"/{audience_id}/users", data=_form(payload)).json()


@mcp.tool()
def remove_users_from_custom_audience(
    audience_id: str,
    schema: list[str],
    users: list[list[str]],
    pre_hashed: bool = False,
) -> dict:
    """Remove people from a CUSTOM (customer list) audience. Same schema/users/
    pre_hashed semantics as add_users_to_custom_audience."""
    data = users if pre_hashed else [_prepare_audience_row(row, schema) for row in users]
    payload = {"payload": {"schema": schema, "data": data}}
    return _request("DELETE", f"/{audience_id}/users", data=_form(payload)).json()


# ---------------------------------------------------------------------------
# Pixels & custom conversions
# ---------------------------------------------------------------------------

@mcp.tool()
def list_pixels(ad_account_id: str | None = None, limit: int = 50) -> list[dict]:
    """List Meta Pixels (conversion tracking pixels) owned by/shared with an ad account."""
    return _paged(
        f"/{_act(ad_account_id)}/adspixels",
        {"fields": "id,name,creation_time,last_fired_time,is_created_by_business"},
        limit,
    )


@mcp.tool()
def get_pixel(pixel_id: str) -> dict:
    """Get a single pixel's details."""
    return _request(
        "GET", f"/{pixel_id}",
        {"fields": "id,name,creation_time,last_fired_time,is_created_by_business,owner_business"},
    ).json()


@mcp.tool()
def list_custom_conversions(ad_account_id: str | None = None, limit: int = 50) -> list[dict]:
    """List custom conversions defined in an ad account."""
    return _paged(
        f"/{_act(ad_account_id)}/customconversions",
        {"fields": "id,name,custom_event_type,pixel_id,rule,creation_time,is_archived"},
        limit,
    )


@mcp.tool()
def get_custom_conversion(custom_conversion_id: str) -> dict:
    """Get a single custom conversion's details."""
    return _request(
        "GET", f"/{custom_conversion_id}",
        {"fields": "id,name,custom_event_type,pixel_id,rule,creation_time,is_archived,default_conversion_value"},
    ).json()


@mcp.tool()
def create_custom_conversion(
    name: str,
    custom_event_type: str,
    ad_account_id: str | None = None,
    pixel_id: str | None = None,
    rule: dict | None = None,
    default_conversion_value: float | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Create a custom conversion derived from pixel events. custom_event_type
    is e.g. 'PURCHASE', 'LEAD', 'COMPLETE_REGISTRATION', 'ADD_TO_CART', 'OTHER'.
    rule is a JSON filter dict matched against pixel events, e.g. {"and":
    [{"event": {"eq": "Purchase"}}, {"url": {"i_contains": "/thank-you"}}]}."""
    payload = {
        "name": name,
        "custom_event_type": custom_event_type,
        "pixel_id": pixel_id,
        "rule": rule,
        "default_conversion_value": default_conversion_value,
    }
    return _request("POST", f"/{_act(ad_account_id)}/customconversions", data=_form(_merge(payload, extra_params))).json()


@mcp.tool()
def delete_custom_conversion(custom_conversion_id: str) -> str:
    """Delete a custom conversion."""
    _request("DELETE", f"/{custom_conversion_id}")
    return f"Deleted custom conversion {custom_conversion_id}"


# ---------------------------------------------------------------------------
# Targeting search
# ---------------------------------------------------------------------------

@mcp.tool()
def search_targeting(
    query: str | None = None,
    targeting_type: str = "adinterest",
    category_class: str | None = None,
    locale: str = "en_US",
    limit: int = 25,
    extra_params: dict | None = None,
) -> list[dict]:
    """Search Meta's targeting taxonomy to find valid values for a targeting
    spec (the targeting dict passed to create_adset/update_adset).
    targeting_type is one of Meta's search types, most commonly:
    - 'adinterest': free-text interest search (pass query); returns {id,
      name, ...} objects to use in targeting.flexible_spec[].interests.
    - 'adTargetingCategory': structured categories; pass category_class (e.g.
      'behaviors', 'demographics', 'life_events', 'industries', 'income',
      'family_statuses') to list that category's options (query is ignored).
    - 'adgeolocation': geo search (pass query, e.g. a city/region/country
      name); results include keys to use in targeting.geo_locations.
    - 'adlocale': language/locale search for targeting.locales.
    For any less common search type, call graph_get('search', {...}) directly."""
    params = {"type": targeting_type, "q": query, "class": category_class, "locale": locale, "limit": limit}
    return _request("GET", "/search", _merge(params, extra_params)).json().get("data", [])


# ---------------------------------------------------------------------------
# Lead ads
# ---------------------------------------------------------------------------

@mcp.tool()
def list_leadgen_forms(page_id: str, limit: int = 50) -> list[dict]:
    """List Lead Ads forms created on a Facebook Page. Requires a token with
    page access to that Page."""
    return _paged(f"/{page_id}/leadgen_forms", {"fields": "id,name,status,created_time,leads_count"}, limit)


@mcp.tool()
def get_leadgen_form(form_id: str) -> dict:
    """Get a single Lead Ads form's details, including its questions."""
    return _request(
        "GET", f"/{form_id}",
        {"fields": "id,name,status,created_time,leads_count,questions,privacy_policy_url"},
    ).json()


@mcp.tool()
def list_leads(form_id: str, limit: int = 50) -> list[dict]:
    """Get leads submitted through a Lead Ads form. Returns personal data
    (field_data) submitted by people - handle it in line with your privacy
    obligations. Requires the leads_retrieval permission."""
    return _paged(f"/{form_id}/leads", {"fields": "id,created_time,field_data,ad_id,adset_id,campaign_id"}, limit)


# ---------------------------------------------------------------------------
# Ad labels
# ---------------------------------------------------------------------------

@mcp.tool()
def list_ad_labels(ad_account_id: str | None = None, limit: int = 100) -> list[dict]:
    """List ad labels defined in an ad account. Attach a label to a campaign/ad
    set/ad/creative by passing adlabels=[{"id": "<label_id>"}] to that object's
    create/update tool (via the named adlabels parameter or extra_params)."""
    return _paged(f"/{_act(ad_account_id)}/adlabels", {"fields": "id,name,created_time"}, limit)


@mcp.tool()
def create_ad_label(name: str, ad_account_id: str | None = None) -> dict:
    """Create a new ad label in an ad account, for organizing/tagging
    campaigns, ad sets, ads, or creatives."""
    return _request("POST", f"/{_act(ad_account_id)}/adlabels", data=_form({"name": name})).json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=BIND_HOST, port=BIND_PORT)
