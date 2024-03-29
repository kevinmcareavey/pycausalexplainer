import itertools
import logging
from abc import ABC, abstractmethod, ABCMeta
from copy import copy

from networkx import DiGraph, topological_sort
from networkx.drawing.nx_agraph import to_agraph

from causal_explainer.utils import powerset, format_dict, powerdict


logger = logging.getLogger("halpern_pearl")
logging.basicConfig(level=logging.INFO)


class Variable:
    def __init__(self, symbol):
        self.symbol = symbol

    def __hash__(self):
        return hash((type(self), self.symbol))

    def __eq__(self, other):
        return self.symbol == other.symbol

    def __str__(self):
        return f"{self.symbol}"

    def __repr__(self):
        return self.__str__()

    def __lt__(self, other):
        return self.symbol < other.symbol


class Event(ABC):
    @abstractmethod
    def entailed_by(self, causal_setting):
        raise NotImplemented

    @abstractmethod
    def variables(self):
        raise NotImplemented

    def __repr__(self):
        return self.__str__()


class PrimitiveEvent(Event):
    def __init__(self, variable, value):
        self.variable = variable
        self.value = value

    def entailed_by(self, causal_setting):
        return causal_setting.values[self.variable] == self.value

    def variables(self):
        return {self.variable}

    def __str__(self):
        return f"{self.variable}={self.value}"


class Negation(Event):
    def __init__(self, child):
        self.child = child

    def entailed_by(self, causal_setting):
        return not self.child.entailed_by(causal_setting)

    def variables(self):
        return self.child.variables()

    def __str__(self):
        return f"!({self.child})"


class BinaryFormula(Event, metaclass=ABCMeta):
    def __init__(self, left_child, right_child):
        self.left_child = left_child
        self.right_child = right_child

    def variables(self):
        return self.left_child.variables() | self.right_child.variables()


class Conjunction(BinaryFormula):
    def entailed_by(self, causal_setting):
        return self.left_child.entailed_by(causal_setting) and self.right_child.entailed_by(causal_setting)

    def __str__(self):
        return f"({self.left_child} & {self.right_child})"


class Disjunction(BinaryFormula):
    def entailed_by(self, causal_setting):
        return self.left_child.entailed_by(causal_setting) or self.right_child.entailed_by(causal_setting)

    def __str__(self):
        return f"({self.left_child} | {self.right_child})"


def assignments2conjunction(assignments, right_child=None):
    assert assignments
    assignments_remainder = copy(assignments)
    variable, value = assignments_remainder.popitem()  # pops items in reverse order, which is important for respecting order of operations
    primitive_event = PrimitiveEvent(variable, value)
    formula = Conjunction(primitive_event, right_child) if right_child else primitive_event
    return assignments2conjunction(assignments_remainder, formula) if assignments_remainder else formula


class CausalNetwork:
    def __init__(self):
        self.graph = DiGraph()

        self.structural_equations = dict()
        self.endogenous_bindings = dict()

    def add_dependency(self, endogenous_variable, parents, structural_equation):
        for parent_variable in parents:
            self.graph.add_edge(parent_variable, endogenous_variable)
        self.structural_equations[endogenous_variable] = structural_equation

    def evaluate(self, context):
        values = copy(context)
        for variable in topological_sort(self.graph):
            if variable not in values:
                values[variable] = self.structural_equation(variable, values)
        return {key: value for key, value in values.items() if key not in context}

    def signature(self):
        in_degrees = self.graph.in_degree()
        return {v for v, d in in_degrees if d == 0}, {v for v, d in in_degrees if d != 0}

    def structural_equation(self, variable, parent_values):
        return self.endogenous_bindings[variable] if variable in self.endogenous_bindings else self.structural_equations[variable](parent_values)

    def intervene(self, intervention):
        new_causal_network = CausalNetwork()
        _, endogenous_variables = self.signature()
        for variable in endogenous_variables:
            new_causal_network.add_dependency(variable, self.graph.predecessors(variable), self.structural_equations[variable])
        for variable, value in intervention.items():
            new_causal_network.endogenous_bindings[variable] = value
        return new_causal_network

    def write(self, path, prog="dot"):  # prog=neato|dot|twopi|circo|fdp|nop
        to_agraph(self.graph).draw(path, prog=prog)


class CausalSetting:
    def __init__(self, causal_network, context, exogenous_domains, endogenous_domains):
        self.causal_network = causal_network
        self.context = context  # dict mapping exogenous variables to values
        self.exogenous_domains = exogenous_domains
        self.endogenous_domains = endogenous_domains

        exogenous_variables, endogenous_variables = self.causal_network.signature()
        assert exogenous_variables == set(self.context.keys())
        assert exogenous_variables == set(self.exogenous_domains.keys())
        assert endogenous_variables == set(self.endogenous_domains.keys())

        assert all(self.context[exogenous_variable] in domain for exogenous_variable, domain in self.exogenous_domains.items())
        self.derived_values = self.causal_network.evaluate(self.context)
        self.values = {**self.context, **self.derived_values}
        assert all(self.values[endogenous_variable] in domain for endogenous_variable, domain in self.endogenous_domains.items())


class CausalFormula:
    def __init__(self, intervention, event):
        self.intervention = intervention  # dict mapping endogenous variables to values
        self.event = event  # Boolean combination of primitive events

    def entailed_by(self, causal_setting):
        new_causal_network = causal_setting.causal_network.intervene(self.intervention)
        new_causal_setting = CausalSetting(new_causal_network, causal_setting.context, causal_setting.exogenous_domains, causal_setting.endogenous_domains)
        return self.event.entailed_by(new_causal_setting)

    def __str__(self):
        return f"[{format_dict(self.intervention, sep_item='; ', sep_key_value='<-', brackets=False)}]({self.event})"


def satisfies_ac1(candidate, event, causal_setting):
    if not candidate:
        return False
    if not assignments2conjunction(candidate).entailed_by(causal_setting):
        return False
    if not event.entailed_by(causal_setting):
        return False
    return True


def find_witnesses_ac2(candidate, event, causal_setting):
    x = {candidate_variable: causal_setting.values[candidate_variable] for candidate_variable in candidate}
    all_w = {other_variable: causal_setting.values[other_variable] for other_variable in causal_setting.endogenous_domains.keys() - candidate.keys()}

    x_variables_tuple = sorted(x.keys())
    x_domains_tuple = [causal_setting.endogenous_domains[variable] - {x[variable]} for variable in x_variables_tuple]  # only consider "remaining" values in domain

    for x_prime_values_tuple in itertools.product(*x_domains_tuple):
        x_prime = {variable: value for variable, value in zip(x_variables_tuple, x_prime_values_tuple)}
        for w in powerdict(all_w):
            witness = {**x_prime, **w}
            casual_formula = CausalFormula(witness, Negation(event))
            if casual_formula.entailed_by(causal_setting):
                yield witness


def satisfies_ac2(candidate, event, causal_setting):
    if not candidate:
        return False
    for _ in find_witnesses_ac2(candidate, event, causal_setting):
        return True  # there is at least one witness
    return False


def is_weak_actual_cause(candidate, event, causal_setting):  # non-minimal actual cause
    if not satisfies_ac1(candidate, event, causal_setting):
        logger.debug("AC1 failed")
        return False
    logger.debug("AC1 passed")
    if not satisfies_ac2(candidate, event, causal_setting):
        logger.debug("AC2 failed")
        return False
    logger.debug("AC2 passed")
    return True


def satisfies_ac3(candidate, event, causal_setting):
    for subset_candidate in powerdict(candidate):
        if subset_candidate != candidate:
            if is_weak_actual_cause(subset_candidate, event, causal_setting):
                return False
    return True


def is_actual_cause(candidate, event, causal_setting):  # as in Halpern (2015) rather than Halpern & Pearl (2005)
    if not is_weak_actual_cause(candidate, event, causal_setting):
        return False
    if not satisfies_ac3(candidate, event, causal_setting):
        logger.debug("AC3 failed")
        return False
    logger.debug("AC3 passed")
    return True


def satisfies_sc1(candidate, event, causal_setting):
    if not assignments2conjunction(candidate).entailed_by(causal_setting):
        return False
    if not event.entailed_by(causal_setting):
        return False
    return True


def satisfies_sc2(candidate, event, causal_setting):
    for actual_cause in search_candidate_causes(event, causal_setting, is_actual_cause):
        for variable, value in candidate.items():
            if variable in actual_cause and actual_cause[variable] == value:  # conjunct variable=value of candidate is part of an actual cause
                return True
    return False


def satisfies_sc3(candidate, event, causal_setting):
    for context_prime in find_exact_assignments(causal_setting.exogenous_domains, causal_setting.exogenous_domains.keys()):
        if not CausalFormula(candidate, event).entailed_by(CausalSetting(causal_setting.causal_network, context_prime, causal_setting.exogenous_domains, causal_setting.endogenous_domains)):
            return False
    return True


def is_weak_sufficient_cause(candidate, event, causal_setting):  # non-minimal sufficient cause
    if not satisfies_sc1(candidate, event, causal_setting):
        return False
    if not satisfies_sc2(candidate, event, causal_setting):
        return False
    if not satisfies_sc3(candidate, event, causal_setting):
        return False
    return True


def satisfies_sc4(candidate, event, causal_setting):
    for subset_candidate in powerdict(candidate):
        if subset_candidate and subset_candidate != candidate:
            if is_weak_sufficient_cause(subset_candidate, event, causal_setting):
                return False
    return True


def is_sufficient_cause(candidate, event, causal_setting):  # as in Halpern (2016) rather than Halpern & Pearl (2005)
    if not is_weak_sufficient_cause(candidate, event, causal_setting):
        return False
    if not satisfies_sc4(candidate, event, causal_setting):
        return False
    return True


def find_exact_assignments(domains, variables):
    assert variables
    variables_tuple = sorted(variables)
    domains_tuple = [domains[variable] for variable in variables_tuple]
    for values_tuple in itertools.product(*domains_tuple):
        yield {variable: value for variable, value in zip(variables_tuple, values_tuple)}


def find_all_assignments(domains):
    for variables in powerset(domains.keys()):
        if variables:
            yield from find_exact_assignments(domains, variables)


def search_candidate_causes(event, causal_setting, condition):
    for candidate in find_all_assignments(causal_setting.endogenous_domains):
        if condition(candidate, event, causal_setting):
            yield candidate
