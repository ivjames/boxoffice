# Data migration: Phase A of the seating-chart epic (docs/SEATING.md).
#
# 0004 added the nullable Performance.seating_chart FK; every row created
# before that migration has it null. This backfills it to whatever
# orders.services.get_seating_chart() already resolved for that performance
# BEFORE the FK existed -- the venue's first SeatingChart, ordered by pk --
# so making the choice explicit doesn't change a single performance's
# behavior. Performances whose venue has no seating chart at all (a GA-only
# venue, or a fixture with no chart yet) are left null, matching
# get_seating_chart()'s "no chart" (None) return for that case.
#
# Deliberately re-implemented here (not imported from orders.services)
# because migrations must not depend on application code that can change
# out from under them -- this uses the historical (as-of-this-migration)
# model via `apps.get_model`, per Django's data-migration guidance.
from django.db import migrations


def backfill_seating_chart(apps, schema_editor):
    Performance = apps.get_model("events", "Performance")
    SeatingChart = apps.get_model("venues", "SeatingChart")

    performances = Performance.objects.filter(seating_chart__isnull=True).select_related("venue")
    for performance in performances:
        chart = (
            SeatingChart.objects.filter(venue_id=performance.venue_id).order_by("pk").first()
        )
        if chart is not None:
            performance.seating_chart_id = chart.pk
            performance.save(update_fields=["seating_chart"])


def noop_reverse(apps, schema_editor):
    # Reversing would mean "forget which chart every performance used" --
    # not meaningfully reversible, and not destructive to leave as a noop
    # (the nullable FK itself is removed by reversing 0004, not this).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0004_performance_seating_chart_and_more"),
        ("venues", "0002_section_arc_radius_section_numbering_scheme_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_seating_chart, noop_reverse),
    ]
