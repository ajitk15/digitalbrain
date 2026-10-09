"""Say which external account a platform user is, where email lookup cannot.

Jira Cloud hides a person's email address unless their profile allows it, and
then nobody is found by email: that member is left out of every issue an
enforcing connector imports. An operator who knows the account can set it here;
a set identity is never overwritten by a lookup. `--clear` removes it.
"""

from django.core.management.base import BaseCommand, CommandError

from platform_core.models import ExternalIdentity, User


class Command(BaseCommand):
    help = "Link a platform user to their account in an external system (Jira)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--site", required=True, help="The site URL, e.g. https://team.atlassian.net"
        )
        parser.add_argument("--account", help="Jira Cloud accountId, or Data Center username")
        parser.add_argument("--clear", action="store_true", help="Remove the link")

    def handle(self, *args, **options):
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError("No such user.")
        site = options["site"].rstrip("/")
        if options["clear"]:
            ExternalIdentity.objects.filter(user=user, system="jira", site=site).delete()
            self.stdout.write("Removed.")
            return
        if not options["account"]:
            raise CommandError("Give --account, or --clear.")
        ExternalIdentity.objects.update_or_create(
            user=user,
            system="jira",
            site=site,
            defaults={"account": options["account"].strip(), "method": "manual"},
        )
        self.stdout.write(f"{user.username} is {options['account']} on {site}.")
