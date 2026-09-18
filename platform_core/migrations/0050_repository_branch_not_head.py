"""Replace the literal "HEAD" stored as a branch name.

`index_repository` used to fall back to `"HEAD"` when nobody named a branch.
That reads correctly on a snapshot line - the clone really did take the remote's
HEAD - but it is not a branch name, and the branch check asks GitHub for
`refs/heads/<name>`. Every one of those calls failed, the failure was swallowed
by design (a branch that cannot be read is not drift), and the repository showed
as never checked with no sign of why.

Indexing now records the branch the clone actually landed on. These rows were
written before that and have nothing to recover it from short of another clone,
so they take the convention - which is what was asked for here - and the next
Refresh index replaces it with the real answer.
"""

from django.db import migrations


def name_the_branch(apps, schema_editor):
    apps.get_model("platform_core", "CodeRepository").objects.filter(
        default_ref="HEAD"
    ).update(default_ref="main")
    # The snapshot line carries the same word, and it is display only - nothing
    # resolves a snapshot by it. Leaving it would put "HEAD at 77c25c99" beside
    # "in sync with main" on one line, which is two names for one branch.
    apps.get_model("platform_core", "CodeSnapshot").objects.filter(ref="HEAD").update(
        ref="main"
    )


def back_to_head(apps, schema_editor):
    """Deliberately not reversible in substance.

    Putting "HEAD" back would restore the bug, and there is no record of which
    rows were "HEAD" because nobody named a branch versus "main" because someone
    did. Doing nothing is the honest reverse.
    """


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0049_coderepository_head_checked_at_and_more")]

    operations = [migrations.RunPython(name_the_branch, back_to_head)]
