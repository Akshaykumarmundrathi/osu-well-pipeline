#!/bin/bash
# Add Secrets Manager access to OSU Batch task role

ROLE_NAME="OSUPipelineBatchTaskRole"
POLICY_NAME="SecretsManagerAccess"
ACCOUNT_ID="225989338968"
REGION="us-east-1"

echo "Adding SecretsManager:GetSecretValue permission to ${ROLE_NAME}..."

# Create policy document
cat > /tmp/secrets-policy.json << 'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:225989338968:secret:osu-pipeline/*"
      ]
    }
  ]
}
POLICY

# Apply policy to role
aws iam put-role-policy \
  --role-name ${ROLE_NAME} \
  --policy-name ${POLICY_NAME} \
  --policy-document file:///tmp/secrets-policy.json \
  --region ${REGION} 2>&1

if [ $? -eq 0 ]; then
  echo "✓ Policy attached successfully"
else
  echo "✗ Failed to attach policy"
  exit 1
fi

# Verify policy
echo ""
echo "Verifying policy..."
aws iam get-role-policy \
  --role-name ${ROLE_NAME} \
  --policy-name ${POLICY_NAME} \
  --region ${REGION} 2>&1 | grep -A 10 '"Statement"'

echo ""
echo "✓ SecretsManager policy update complete"
