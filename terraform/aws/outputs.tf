output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.mcp_server.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.mcp_server.arn
}

output "cloudwatch_log_group" {
  description = "CloudWatch Log Group name"
  value       = aws_cloudwatch_log_group.lambda_logs.name
}

output "api_gateway_access_log_group" {
  description = "CloudWatch Log Group for API Gateway access logs (distinct-users analysis)"
  value       = aws_cloudwatch_log_group.api_gateway_access.name
}

output "api_gateway_url" {
  description = "API Gateway URL for MCP server (public production endpoint)"
  value       = "${aws_api_gateway_stage.prod.invoke_url}/mcp"
}

# ── Custom domain outputs (only populated when var.custom_domain is set) ────
#
# DNS for codeforanchorage.org is managed outside Terraform, so a custom-domain
# deploy takes TWO DNS records, both emitted here:
#
#   1. acm_validation_cname_{name,value} — proves domain control to ACM. The
#      cert stays PENDING_VALIDATION (and the apply blocks) until this record
#      resolves, so create it first.
#   2. audubon-iba.codeforanchorage.org CNAME → custom_domain_target — routes
#      real traffic to the API Gateway regional endpoint.

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate for the custom domain"
  value       = var.custom_domain != "" ? aws_acm_certificate.mcp_cert[0].arn : null
}

output "acm_validation_cname_name" {
  description = "DNS CNAME record name to create in codeforanchorage.org for ACM validation"
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_name : null
}

output "acm_validation_cname_value" {
  description = "DNS CNAME record value for ACM validation"
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_value : null
}

output "custom_domain_target" {
  description = "API Gateway regional domain name — point the custom domain's CNAME here"
  value       = var.custom_domain != "" ? aws_api_gateway_domain_name.custom[0].regional_domain_name : null
}

output "mcp_url" {
  description = "The public MCP endpoint to paste into Claude Connectors (custom domain when set, else the raw API Gateway URL)"
  value       = var.custom_domain != "" ? "https://${var.custom_domain}/mcp" : "${aws_api_gateway_stage.prod.invoke_url}/mcp"
}
