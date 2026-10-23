"""Hosted services and scheduled jobs (private package; the public surface is ``lauren.scheduling``).

* ``_decorators`` — ``@hosted_service``, ``@interval``, ``@cron``, ``@run_once``
  and the discovery marker registration.
* ``_cron`` — ``CronExpression`` and time-zone helpers.
* ``_clock`` — ``Clock``, ``SystemClock``, ``ManualClock``.
* ``_context`` — ``JobContext``, ``ServiceContext``.
* ``_lock`` — ``SchedulerLock``, ``InMemorySchedulerLock``.
* ``_scheduler`` — the per-app scheduler, ``SchedulerRegistry``, ``JobInfo``, ``JobRun``.
* ``_supervisor`` — the hosted-service supervisor, ``HostedServiceInfo``.
* ``_module`` — ``ScheduleModule`` and the ``LaurenFactory.create`` phases.
"""

from __future__ import annotations
