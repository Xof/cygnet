# apps.py — Django AppConfig for the cross-ORM comparison benchmarks.
#
# Django demands that every model belong to an app listed in
# INSTALLED_APPS, even one that exists purely to host benchmark models.
# This file is the smallest possible Django app — just enough metadata
# for Django's app registry to accept comparison.models.

from __future__ import annotations

from django.apps import AppConfig


class ComparisonConfig(AppConfig):
    name = "bench.comparison"
    label = "comparison"
    default_auto_field = "django.db.models.AutoField"
