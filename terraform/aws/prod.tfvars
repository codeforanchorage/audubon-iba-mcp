lambda_name = "audubon-iba-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"

# 512 MB is plenty: the plugin holds no catalog in memory (the service is a
# single fixed FeatureServer) and every query streams straight through to
# ArcGIS. No polygon math happens in-process -- the point buffer in
# find_ibas_near_point is computed server-side by ArcGIS.
lambda_memory = 512

# The IBA service is a third-party host we do not control. 120s leaves room
# for a slow upstream plus a couple of pagination round-trips.
lambda_timeout = 28

api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10

custom_domain = "audubon-iba.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
# This also bounds the load we can put on Audubon's public endpoint.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
waf_rate_limit_per_5min = 300

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 300/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `audubon-iba` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
