from ray.rllib.models.tf.tf_modelv2 import TFModelV2
from ray.rllib.utils import try_import_tf

tf = try_import_tf()


class DDPGModel(TFModelV2):
    """Extension of standard TFModel to provide DDPG action- and q-outputs.

    Data flow:
        obs -> forward() -> model_out
        model_out -> get_policy_output() -> deterministic actions
        model_out, actions -> get_q_values() -> Q(s, a)
        model_out, actions -> get_twin_q_values() -> Q_twin(s, a)

    Note that this class by itself is not a valid model unless you
    implement forward() in a subclass."""

    def __init__(
            self,
            obs_space,
            action_space,
            num_outputs,
            model_config,
            name,
            # Extra DDPGActionModel args:
            actor_hiddens=(256, 256),
            actor_hidden_activation="relu",
            critic_hiddens=(256, 256),
            critic_hidden_activation="relu",
            twin_q=False,
            add_layer_norm=False):
        """Initialize variables of this model.

        Extra model kwargs:
            actor_hiddens (list): Defines size of hidden layers for the DDPG
                policy head.
                These will be used to postprocess the model output for the
                purposes of computing deterministic actions.

        Note that the core layers for forward() are not defined here, this
        only defines the layers for the DDPG head. Those layers for forward()
        should be defined in subclasses of DDPGActionModel.
        """

        super(DDPGModel, self).__init__(obs_space, action_space, num_outputs,
                                        model_config, name)

        actor_hidden_activation = getattr(tf.nn, actor_hidden_activation,
                                          tf.nn.relu)
        critic_hidden_activation = getattr(tf.nn, critic_hidden_activation,
                                           tf.nn.relu)

        self.model_out = tf.keras.layers.Input(
            shape=(num_outputs, ), name="model_out")
        self.action_dim = action_space.shape[0]

        if actor_hiddens:
            last_layer = self.model_out
            for i, n in enumerate(actor_hiddens):
                last_layer = tf.keras.layers.Dense(
                    n,
                    name="actor_hidden_{}".format(i),
                    activation=actor_hidden_activation)(last_layer)
                if add_layer_norm:
                    last_layer = tf.keras.layers.LayerNormalization(
                        name="LayerNorm_{}".format(i))(last_layer)
            actor_out = tf.keras.layers.Dense(
                self.action_dim, activation=None, name="actor_out")(last_layer)
        else:
            actor_out = self.model_out

        # Use sigmoid to scale to [0,1], but also double magnitude of input to
        # emulate behaviour of tanh activation used in DDPG and TD3 papers.
        def lambda_(x):
            sigmoid_out = tf.nn.sigmoid(2 * x)
            # Rescale to actual env policy scale
            # (shape of sigmoid_out is [batch_size, dim_actions], so we reshape
            # to get same dims)
            action_range = (action_space.high - action_space.low)[None]
            low_action = action_space.low[None]
            actions = action_range * sigmoid_out + low_action
            return actions

        actor_out = tf.keras.layers.Lambda(lambda_)(actor_out)

        self.action_model = tf.keras.Model(self.model_out, actor_out)
        self.register_variables(self.action_model.variables)

        # Build the Q-model(s).
        self.actions_input = tf.keras.layers.Input(
            shape=(self.action_dim, ), name="actions")

        def build_q_net(name, observations, actions):
            # For continuous actions: Feed obs and actions (concatenated)
            # through the NN.
            q_net = tf.keras.Sequential([
                tf.keras.layers.Concatenate(axis=1),
            ] + [
                tf.keras.layers.Dense(
                    units=units,
                    activation=critic_hidden_activation,
                    name="{}_hidden_{}".format(name, i))
                for i, units in enumerate(critic_hiddens)
            ] + [
                tf.keras.layers.Dense(
                    units=1, activation=None, name="{}_out".format(name))
            ])

            q_net = tf.keras.Model([observations, actions],
                                   q_net([observations, actions]))
            return q_net

        self.q_net = build_q_net("q", self.model_out, self.actions_input)
        self.register_variables(self.q_net.variables)

        if twin_q:
            self.twin_q_net = build_q_net("twin_q", self.model_out,
                                          self.actions_input)
            self.register_variables(self.twin_q_net.variables)
        else:
            self.twin_q_net = None

    def get_q_values(self, model_out, actions):
        """Return the Q estimates for the most recent forward pass.

        This implements Q(s, a).

        Arguments:
            model_out (Tensor): obs embeddings from the model layers, of shape
                [BATCH_SIZE, num_outputs].
            actions (Tensor): Actions to return the Q-values for.
                Shape: [BATCH_SIZE, action_dim].

        Returns:
            tensor of shape [BATCH_SIZE].
        """
        if actions is not None:
            return self.q_net([model_out, actions])
        else:
            return self.q_net(model_out)

    def get_twin_q_values(self, model_out, actions):
        """Same as get_q_values but using the twin Q net.

        This implements the twin Q(s, a).

        Arguments:
            model_out (Tensor): obs embeddings from the model layers, of shape
                [BATCH_SIZE, num_outputs].
            actions (Tensor): Actions to return the Q-values for.
                Shape: [BATCH_SIZE, action_dim].

        Returns:
            tensor of shape [BATCH_SIZE].
        """
        if actions is not None:
            return self.twin_q_net([model_out, actions])
        else:
            return self.twin_q_net(model_out)

    def get_policy_output(self, model_out):
        """Return the action output for the most recent forward pass.

        This outputs the support for pi(s). For continuous action spaces, this
        is the action directly.

        Arguments:
            model_out (Tensor): obs embeddings from the model layers, of shape
                [BATCH_SIZE, num_outputs].

        Returns:
            tensor of shape [BATCH_SIZE, action_out_size]
        """
        return self.action_model(model_out)

    def policy_variables(self):
        """Return the list of variables for the policy net."""
        return list(self.action_model.variables)

    def q_variables(self):
        """Return the list of variables for Q / twin Q nets."""

        return self.q_net.variables + (self.twin_q_net.variables
                                       if self.twin_q_net else [])
