"""Link a platform user to their Microsoft Entra ID account, by object id.

A first sign-in links by sign-in name on its own, except for a platform
administrator, a name that matches more than one account, or a user whose name
here differs from their Entra sign-in name. This is how an operator links
those. The object id is the account's "Object ID" in the Entra admin centre
(Users -> the person -> Overview). `--clear` removes the link, after which the
next sign-in matches by name again.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from digitalbrain.configuration import GUID
from platform_core.models import SignInIdentity, User
from platform_core.services import audit


class Command(BaseCommand):
    help = "Link a platform user to their Microsoft Entra ID account (single sign-on)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--object-id", help="The account's Entra object id (a GUID)")
        parser.add_argument("--clear", action="store_true", help="Remove the link")

    def handle(self, *args, **options):
        tenant = settings.ENTRA_TENANT
        if not tenant:
            raise CommandError("Single sign-on is not configured: set entra_tenant first.")
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError("No such user.")
        if options["clear"]:
            removed, _ = SignInIdentity.objects.filter(user=user, tenant=tenant).delete()
            if removed:
                details = {"provider": "entra", "tenant": tenant}
                audit(user, "sso_unlinked", user.pk, details=details)
            self.stdout.write("Removed." if removed else "Nothing was linked.")
            return
        subject = (options["object_id"] or "").strip().lower()
        if not GUID.fullmatch(subject):
            raise CommandError("Give --object-id as a GUID, or --clear.")
        holder = SignInIdentity.objects.filter(tenant=tenant, subject=subject).first()
        if holder and holder.user_id != user.pk:
            raise CommandError(
                f"That Microsoft account is linked to {holder.user.username}. Clear it first."
            )
        SignInIdentity.objects.update_or_create(
            user=user, tenant=tenant, defaults={"subject": subject, "method": "manual"}
        )
        audit(
            user,
            "sso_linked",
            user.pk,
            details={"provider": "entra", "tenant": tenant, "method": "manual"},
        )
        self.stdout.write(f"{user.username} signs in as Entra object {subject}.")
