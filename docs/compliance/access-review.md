# Access review

**Quarterly**, and whenever someone leaves. The reviewer is not the person whose access is reviewed.

1. List project IAM for `hexera-prod` and `hexera-dev`:
   `gcloud projects get-iam-policy hexera-prod --format=json > iam-prod-<date>.json`
2. List IAP access to the admin console, GitHub organisation members and repository admins, the
   GitHub `production` environment's required reviewers, Stripe dashboard users, and Secret
   Manager accessors.
3. For each principal: still needed? least privilege? Remove what is not, and note the change.
4. Confirm `ADMIN_API_KEY`, `MESH_API_KEY` and `USER_TOKEN_SECRET` were rotated within the last
   year, or on any departure of someone who could read them.
5. Record in the evidence log: date, reviewer, systems covered, changes made. Keep the exports
   with the record.
