# Backend resources for the Audubon IBA fork.
#
# Run ONCE, before the first `scripts/deploy.sh`:
#   cd terraform/bootstrap
#   terraform init
#   terraform apply -var-file=audubon-iba.tfvars
#
# The bucket name here must match `bucket` in terraform/aws/backend.tf, and
# the region must match `region` there. Both are marked prevent_destroy.

aws_region        = "us-west-2"
state_bucket_name = "audubon-iba-opencontext-tfstate"

# Unused by terraform/aws (backend.tf uses S3-native `use_lockfile` locking
# rather than a DynamoDB table), but the bootstrap module still creates it.
lock_table_name = "audubon-iba-terraform-state-lock"
