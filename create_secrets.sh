#!/bin/bash
# Create AWS Secrets Manager entries for OSU pipeline
# NOTE: Replace ACTUAL_API_KEY and ACTUAL_PASSWORD with real values

echo "Creating Secrets Manager entries for OSU pipeline..."

# 1. Gemini API Key
echo -n "Enter Google Gemini API Key (or press Enter to skip): "
read GEMINI_KEY

if [ ! -z "$GEMINI_KEY" ]; then
  aws secretsmanager create-secret \
    --name osu-pipeline/gemini-api-key \
    --description "Google Gemini API Key for OSU pipeline" \
    --secret-string "{\"api_key\":\"$GEMINI_KEY\"}" \
    --region us-east-1 2>&1 | grep -E "ARN|Error"
  echo "✓ Created osu-pipeline/gemini-api-key"
else
  echo "⚠ Skipped Gemini API Key"
fi

# 2. RDS Credentials
echo -n "Enter RDS password (or press Enter to skip): "
read -s RDS_PASS
echo ""

if [ ! -z "$RDS_PASS" ]; then
  aws secretsmanager create-secret \
    --name osu-pipeline/rds \
    --description "PostgreSQL credentials for PLSS lookup" \
    --secret-string "{
      \"host\": \"oklahomagridlatlongdb.cz62c0sysryk.us-east-1.rds.amazonaws.com\",
      \"port\": 5432,
      \"username\": \"LookUpMaster\",
      \"password\": \"$RDS_PASS\",
      \"dbname\": \"Oklahomaplss\"
    }" \
    --region us-east-1 2>&1 | grep -E "ARN|Error"
  echo "✓ Created osu-pipeline/rds"
else
  echo "⚠ Skipped RDS credentials"
fi

echo ""
echo "Verifying created secrets..."
aws secretsmanager list-secrets --region us-east-1 --filters Key=name,Values=osu-pipeline --query 'SecretList[*].Name' --output text

