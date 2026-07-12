terraform {
  backend "s3" {
    # Per-fork state bucket. This fork deploys its own Lambda, API Gateway,
    # and domain, so it must NOT share state with the sibling GIS forks --
    # a shared bucket+key would have each deploy destroy the other's stack.
    # Create it once via terraform/bootstrap (see bootstrap/audubon-iba.tfvars).
    bucket       = "audubon-iba-opencontext-tfstate"
    key          = "terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true # S3-native state locking (replaces deprecated dynamodb_table)
    encrypt      = true
  }
}
