def _map_os_platform():
    # ruleid: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except IntegrityError:
        recover_winner()


def _map_os_platform():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.full_clean()
        mapping.save()
    except (ValidationError, IntegrityError):
        recover_winner()


def other_operation():
    # ok: mapping-create-recovers-validation-races
    try:
        mapping.save()
    except IntegrityError:
        recover_winner()
