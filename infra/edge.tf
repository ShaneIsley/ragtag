# CloudFront gives the app an HTTPS address (*.cloudfront.net) without a custom
# domain; Entra sign-in needs HTTPS redirect URIs. CloudFront signs every
# request to the Lambda function URL with origin access control (SigV4), and
# the function URL accepts only requests signed for this distribution. For a
# POST, the browser sends the body's SHA-256 in x-amz-content-sha256 so
# CloudFront can sign it (app/templates/index.html).
#
# Everything here exists once image_tag is set.

resource "aws_cloudfront_origin_access_control" "app" {
  count                             = local.deployed ? 1 : 0
  name                              = "${local.name}-app"
  description                       = "Signs requests to the app's Lambda function URL"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Forwards the viewer's headers, cookies and query strings, but not Host:
# the signature is for the function URL's own host name.
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

locals {
  function_url_host = local.deployed ? trimsuffix(trimprefix(aws_lambda_function_url.app[0].function_url, "https://"), "/") : ""
}

resource "aws_cloudfront_distribution" "app" {
  count           = local.deployed ? 1 : 0
  enabled         = true
  comment         = "${local.name}: HTTPS front door"
  price_class     = "PriceClass_All"
  http_version    = "http2and3"
  is_ipv6_enabled = true

  origin {
    origin_id                = "app"
    domain_name              = local.function_url_host
    origin_access_control_id = aws_cloudfront_origin_access_control.app[0].id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }

  default_cache_behavior {
    target_origin_id         = "app"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # The default *.cloudfront.net certificate. A custom domain with an ACM
  # certificate would allow minimum_protocol_version = "TLSv1.2_2021".
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# Function URLs with AWS_IAM auth need both permissions for the caller.
resource "aws_lambda_permission" "cloudfront_url" {
  count                  = local.deployed ? 1 : 0
  statement_id           = "AllowCloudFrontInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.app[0].function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.app[0].arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  count         = local.deployed ? 1 : 0
  statement_id  = "AllowCloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app[0].function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.app[0].arn
}
