terraform {
  # Supply the non-secret values from backend.s3.tfbackend at init time.
  # The state bucket is deliberately provisioned outside this root module.
  backend "s3" {}
}
