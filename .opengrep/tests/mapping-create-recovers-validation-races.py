def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except IntegrityError:
        recover_winner()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except IntegrityError as exc:
        recover_winner()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (IntegrityError, OperationalError):
        recover_winner()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (OperationalError, IntegrityError) as exc:
        recover_winner()


def _map_os_platform():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (ValidationError, IntegrityError):
        recover_winner()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except IntegrityError:
        recover_winner()
    else:
        log_created()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (OperationalError, IntegrityError):
        recover_winner()
    finally:
        release()


def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (IntegrityError, OperationalError) as exc:
        recover_winner()
    else:
        log_created()
    finally:
        release()


def _map_os_platform():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (ValidationError, IntegrityError):
        recover_winner()
    else:
        log_created()


def _map_os_platform():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (ValidationError, IntegrityError) as exc:
        recover_winner()
    finally:
        release()


def _map_os_platform():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (IntegrityError, ValidationError):
        recover_winner()
    else:
        log_created()
    finally:
        release()


def other_operation():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.save()
    except IntegrityError:
        recover_winner()
