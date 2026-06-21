# models.py — Django models that mirror the bench schema.
#
# `managed = False` because the schema is owned by Cygnet's bench
# fixture (bench/conftest.py:populated_db creates the tables); Django
# is just a read/write client against the same `accounts` and `posts`
# tables.  Field shape and types match Cygnet's dataclasses so the
# comparison benchmarks measure equivalent work.

from __future__ import annotations

from django.db import models


class DjangoAccount(models.Model):
    name = models.CharField(max_length=100)
    email = models.CharField(max_length=100)

    class Meta:
        app_label = "comparison"
        db_table = "accounts"
        managed = False


class DjangoPost(models.Model):
    account = models.ForeignKey(
        DjangoAccount,
        on_delete=models.CASCADE,
        db_column="account_id",
    )
    title = models.CharField(max_length=200)
    body = models.TextField()

    class Meta:
        app_label = "comparison"
        db_table = "posts"
        managed = False
