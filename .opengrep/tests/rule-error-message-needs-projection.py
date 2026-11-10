def module_refusal(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": str(error)}


def module_refusal_as_repr(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": repr(error)}


def module_refusal_in_an_f_string(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": f"Refused: {error}"}


def module_refusal_through_format(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": "Refused: {}".format(error)}


def module_refusal_from_args(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": error.args[0]}


def public_module_refusal(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ok: rule-error-message-needs-projection
        return {"message": error.public_message}


def public_module_refusal_in_an_f_string(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ok: rule-error-message-needs-projection
        return {"message": f"Refused: {error.public_message}"}


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, str(exception))


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, repr(exception))


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, f"{exception}")


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, "{}: {}".format(request.path, exception))


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, exception.args)


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ok: rule-error-message-needs-projection
        return answer(request, exception.public_message)


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ok: rule-error-message-needs-projection
        logger.error("Rule configuration error in %s: %s", request.path, exception)


def ordinary_value(value):
    # ok: rule-error-message-needs-projection
    return str(value)
