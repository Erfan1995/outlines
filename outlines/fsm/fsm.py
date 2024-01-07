from typing import TYPE_CHECKING, List, NewType, Protocol, Tuple

import interegular
from lark import Lark

# from outlines.fsm.parsing import PartialLark
from outlines.caching import cache
from outlines.fsm.regex import create_fsm_index_tokenizer, make_deterministic_fsm

if TYPE_CHECKING:
    from outlines.models.tokenizer import Tokenizer

FSMState = NewType("FSMState", int)


class FSM(Protocol):
    def allowed_token_ids(self, state: FSMState) -> List[int]:
        ...

    def next_state(self, state: FSMState, token_id: int) -> FSMState:
        ...

    def is_final_state(self, state: FSMState) -> bool:
        ...

    def copy(self) -> "FSM":
        ...


class StopAtTokenFSM(FSM):
    """FSM to generate text until a specified token id is generated or
    a specified number of tokens has been generated.

    Text is usually produced until the EOS token is generated by the
    model.

    """

    def __init__(self, tokenizer: "Tokenizer", stop_token_id: int):
        self.stop_token_id = stop_token_id
        self.vocabulary = tokenizer.vocabulary.values()
        self.final_states = {1}

    def allowed_token_ids(self, state: FSMState) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        When in the initial state we allow every token to be generated.
        In the final state the only allowed token is `stop_token_id`.

        Parameters
        ----------
        state
            The current state of the FSM.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        if state == 0:
            return list(self.vocabulary)
        else:
            return [self.stop_token_id]

    def next_state(self, state: FSMState, token_id: int) -> FSMState:
        """Update the state of the FSM.

        The FSM stays in the initial state `0` unless the specified stop token
        has been generated or the maximum number of tokens has been reached. In
        which case the FSM moves to the final state `1`.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.

        Returns
        -------
        The new state of the FSM.

        """
        if token_id == self.stop_token_id:
            return FSMState(1)

        return FSMState(0)

    def is_final_state(self, state: FSMState) -> bool:
        """Determine whether the current state of the FSM is a final state."""
        return state in self.final_states

    def copy(self) -> "StopAtTokenFSM":
        """Create a copy of the FSM."""
        return self


class RegexFSM(FSM):
    """FSM to generate text that is in the language of a regular expression."""

    def __init__(
        self,
        regex_string: str,
        tokenizer: "Tokenizer",
    ):
        def func_cache_key_args(
            regex_string: str, tokenizer: "Tokenizer"
        ) -> Tuple[str, tuple]:
            """Return the values that will be used to create the cache key of create_states_mapping"""
            cacheable_vocabulary = tuple(sorted(tokenizer.vocabulary.items()))
            return (regex_string, cacheable_vocabulary)

        @cache(func_cache_key_args)
        def create_states_mapping(
            regex_string: str, tokenizer: "Tokenizer"
        ) -> Tuple[dict, set, set]:
            """Create the variables related to the mapping between states and tokens
            The parameters of the function are used for caching purpose
            """
            regex_pattern = interegular.parse_pattern(regex_string)
            regex_fsm, _ = make_deterministic_fsm(regex_pattern.to_fsm().reduce())
            (
                states_to_token_maps,
                empty_token_ids,
            ) = create_fsm_index_tokenizer(regex_fsm, tokenizer)

            # We make sure that it is possible to generate strings in the language
            # of the regular expression with the tokens present in the model's
            # vocabulary.
            if not any(
                regex_fsm.finals.intersection(v.values())
                for v in states_to_token_maps.values()
            ):
                raise ValueError(
                    "The vocabulary does not allow us to build a sequence that matches the input regex"
                )

            final_states = regex_fsm.finals | {
                -1
            }  # Include the EOS token in final states
            return states_to_token_maps, empty_token_ids, final_states

        (
            self.states_to_token_maps,
            self.empty_token_ids,
            self.final_states,
        ) = create_states_mapping(regex_string, tokenizer)
        self.num_tokens_generated = 0
        self.vocabulary = tokenizer.vocabulary.values()
        self.end_token_id = tokenizer.eos_token_id

    def allowed_token_ids(self, state: FSMState) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        The initialization of the FSM builds an index which maps FSM states to a
        map from authorized tokens to the state in which the FSM needs to move
        if said token is generated. Therefore the authorized tokens at the
        current state are the keys of the map returned by the value of the index
        for current state.

        If the current state is not contained in the end this means that we are
        in a final state of the FSM. We only authorize EOS tokens in the final
        state.

        Parameters
        ----------
        state
            The current state of the FSM.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        next_tokens_to_end_states = self.states_to_token_maps.get(state)

        if next_tokens_to_end_states is None:
            return [self.end_token_id]
        else:
            return list(next_tokens_to_end_states.keys())

    def next_state(self, state: FSMState, token_id: int) -> FSMState:
        """Update the state of the FSM.

        We use the index to determine to which state the FSM should transition
        given the token that was just generated.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.

        Returns
        -------
        The new state of the FSM.

        """
        if token_id == self.end_token_id:
            return FSMState(-1)

        last_token_to_end_state = self.states_to_token_maps[state]
        next_state = last_token_to_end_state.get(token_id)
        if next_state is None:
            next_state = -1

        return FSMState(next_state)

    def is_final_state(self, state: FSMState) -> bool:
        """Determine whether the current state of the FSM is a final state."""
        return state in self.final_states

    def copy(self) -> "RegexFSM":
        """Create a copy of the FSM."""
        return self


class CFGFSM(FSM):
    """FSM to generate text that is in the language of a context-free grammar."""

    def __init__(self, cfg_string: str, tokenizer: "Tokenizer"):
        self.cfg_string = cfg_string
        self.tokenizer = tokenizer

        self.parser = Lark(
            cfg_string,
            parser="lalr",
            lexer="contextual",
            propagate_positions=False,
            maybe_placeholders=False,
            regex=True,
        )
        self.terminal_regexps = dict()
        for terminal in self.parser.terminals:
            if terminal.pattern is not None:
                self.terminal_regexps[terminal.name] = terminal.pattern.to_regexp()
        self.terminal_regexps["$END"] = tokenizer.eos_token

        self.generation = ""
        self.reset_state = False
        self.allow_eos = False
        self.done = False
        self.regex_fsm: RegexFSM

    def _set_next_regex_fsm(self) -> None:
        """Use the CFG incremental parser to set the next regex FSM.

        Check what the CFG incremental parser proposes next:
        - If the only proposal is the EOS token we set the state to done and
          return.
        - If there are other proposals, we set a new regex FSM and return.

        """
        interactive = self.parser.parse_interactive(self.generation)
        interactive.exhaust_lexer()
        options = {self.terminal_regexps[x] for x in interactive.accepts()}

        if self.terminal_regexps["$END"] in options:
            options.remove(self.terminal_regexps["$END"])
            if len(options) == 0:
                self.done = True
                return
            self.allow_eos = True
            options.add("")
            assert len(options) > 1

        regex_string = r"(" + r"|".join([r"(" + x + r")" for x in options]) + r")"
        self.regex_fsm = RegexFSM(regex_string, self.tokenizer)
        self.reset_state = True

    def allowed_token_ids(self, state: FSMState) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        Upon initialization, the CFG incremental parser is used to determine the
        first regex.

        This regex is used for proposals until either:

        - The regex is exhausted, and its only remaining option is the EOS
          token, in which case we always transition to the next regex
        - The regex can be exhausted, but the EOS token is not the only
          remaining option, in which case we transition to the next regex with
          probability P (TODO) or remove the possibility of generating the EOS
          token and continue with the current regex

        The CFG incremental parser is allowed to propose the EOS token from any final state,
        and once it is generated, the FSM will continue to always generate the EOS token.

        Parameters
        ----------
        state
            The current state of the FSM.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        if self.generation != "":
            proposal = self.regex_fsm.allowed_token_ids(state)
            if self.tokenizer.eos_token_id not in proposal:
                return proposal
            if set(proposal) != {self.tokenizer.eos_token_id}:
                if False:  # TODO: THIS NEEDS TO BE SAMPLED
                    proposal = [x for x in proposal if x != self.tokenizer.eos_token_id]
                    return proposal

        self._set_next_regex_fsm()

        if self.done:
            return [self.tokenizer.eos_token_id]

        if self.reset_state:
            state = FSMState(0)

        proposal = self.regex_fsm.allowed_token_ids(state)
        if self.allow_eos:
            self.allow_eos = False
        else:
            proposal = [x for x in proposal if x != self.tokenizer.eos_token_id]
            assert len(proposal) > 0
        return proposal

    def next_state(self, state: FSMState, token_id: int) -> FSMState:
        """Update the state of the FSM.

        Transitions the underlying regex FSM to its next state.
        If at max tokens or EOS token, transition permanently to the final state.
        Update stored partial generations for subsequent incremental parsing.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.

        Returns
        -------
        The new state of the FSM.
        """
        if token_id == self.tokenizer.eos_token_id:
            self.done = True
            return FSMState(-1)
        if self.reset_state:
            self.reset_state = False
            state = FSMState(0)

        self.generation += self.tokenizer.decode([token_id])[0]

        return self.regex_fsm.next_state(state, token_id)

    def is_final_state(self, state: FSMState) -> bool:
        """Return whether the current state of the FSM is a final state."""
        return self.done

    def copy(self) -> "CFGFSM":
        """Create a copy of the FSM."""
        return CFGFSM(self.cfg_string, self.tokenizer)
