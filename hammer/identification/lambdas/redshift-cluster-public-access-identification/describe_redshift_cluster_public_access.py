import json
import logging

from library.logger import set_logging
from library.config import Config
from library.aws.redshift import RedshiftClusterPublicAccessChecker
from library.aws.utility import Account
from library.ddb_issues import IssueStatus, RedshiftPublicAccessIssue
from library.ddb_issues import Operations as IssueOperations
from library.aws.utility import Sns


def lambda_handler(event, context):
    """ Lambda handler to evaluate Redshift cluster publicly accessible or not. """
    set_logging(level=logging.DEBUG)

    try:
        payload = json.loads(event["Records"][0]["Sns"]["Message"])
        account_id = payload['account_id']
        account_name = payload['account_name']
        # get the last region from the list to process
        region = payload['regions'].pop()
        # region = payload['region']
    except Exception:
        logging.exception(f"Failed to parse event\n{event}")
        return

    try:
        config = Config()

        main_account = Account(region=config.aws.region)
        ddb_table = main_account.resource("dynamodb").Table(config.redshift_public_access.ddb_table_name)

        account = Account(id=account_id,
                          name=account_name,
                          region=region,
                          role_name=config.aws.role_name_identification)
        if account.session is None:
            return

        logging.debug(f"Checking for publicly accessible Redshift clusters in {account}")

        # existing open issues for account to check if resolved
        open_issues = IssueOperations.get_account_open_issues(ddb_table, account_id, RedshiftPublicAccessIssue)
        # make dictionary for fast search by id
        # and filter by current region
        open_issues = {issue.issue_id: issue for issue in open_issues if issue.issue_details.region == region}
        logging.debug(f"Redshift clusters in DDB:\n{open_issues.keys()}")

        checker = RedshiftClusterPublicAccessChecker(account=account)
        if checker.check():
            for cluster in checker.clusters:
                logging.debug(f"Checking {cluster.name}")
                if cluster.is_public:
                    issue = RedshiftPublicAccessIssue(account_id, cluster.name)
                    issue.issue_details.tags = cluster.tags
                    issue.issue_details.region = cluster.account.region
                    if config.redshiftEncrypt.in_whitelist(account_id, cluster.name):
                        issue.status = IssueStatus.Whitelisted
                    else:
                        issue.status = IssueStatus.Open
                    logging.debug(f"Setting {cluster.name} status {issue.status}")
                    IssueOperations.update(ddb_table, issue)
                    # remove issue id from issues_list_from_db (if exists)
                    # as we already checked it
                    open_issues.pop(cluster.name, None)

        logging.debug(f"Redshift Clusters in DDB:\n{open_issues.keys()}")
        # all other unresolved issues in DDB are for removed/remediated clusters
        for issue in open_issues.values():
            IssueOperations.set_status_resolved(ddb_table, issue)
    except Exception:
        logging.exception(f"Failed to check Redshift clusters for '{account_id} ({account_name})'")
        return

    # push SNS messages until the list with regions to check is empty
    if len(payload['regions']) > 0:
        try:
            Sns.publish(payload["sns_arn"], payload)
        except Exception:
            logging.exception("Failed to chain public access issues checking")

    logging.debug(f"Checked Redshift Clusters for '{account_id} ({account_name})'")


