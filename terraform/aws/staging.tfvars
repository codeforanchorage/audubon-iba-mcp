lambda_name = "audubon-iba-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"

lambda_memory  = 512
lambda_timeout = 120

# Tighter than prod -- staging is for smoke tests, not traffic.
api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

# No custom domain in staging: reach it via the raw API Gateway invoke URL
# (printed by deploy.sh, and available as the api_gateway_url output).
# Leaving this empty is what makes custom_domain.tf skip the ACM cert and
# domain mapping entirely.
custom_domain = ""

lambda_reserved_concurrency = 5
waf_rate_limit_per_5min     = 300
