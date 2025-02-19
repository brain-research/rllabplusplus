

import numpy as np
from rllab.misc import tensor_utils
from rllab.misc import special2 as special
from rllab.algos import util
import rllab.misc.logger as logger

class Sampler(object):
    def start_worker(self):
        """
        Initialize the sampler, e.g. launching parallel workers if necessary.
        """
        raise NotImplementedError

    def obtain_samples(self, itr):
        """
        Collect samples for the given iteration number.
        :param itr: Iteration number.
        :return: A list of paths.
        """
        raise NotImplementedError

    def process_samples(self, itr, paths):
        """
        Return processed sample data (typically a dictionary of concatenated tensors) based on the collected paths.
        :param itr: Iteration number.
        :param paths: A list of collected paths.
        :return: Processed sample data.
        """
        raise NotImplementedError

    def shutdown_worker(self):
        """
        Terminate workers if necessary.
        """
        raise NotImplementedError


class BaseSampler(Sampler):
    def __init__(self, algo):
        """
        :type algo: BatchPolopt
        """
        self.algo = algo

    def process_advantages(self, advantages):
        if self.algo.center_adv:
            advantages, adv_std = util.center_advantages(advantages)
            return advantages, adv_std

        if self.algo.positive_adv:
            advantages = util.shift_advantages_to_positive(advantages)
            return advantages

    def process_samples(self, itr, paths):
        advantage_baselines = []
        baselines = []
        returns = []

        if hasattr(self.algo.baseline, "predict_n"):
            all_path_baselines = self.algo.baseline.predict_n(paths)
        else:
            all_path_baselines = [self.algo.baseline.predict(path) for path in paths]

        # all_path_advantages = [self.algo.extra_baseline.predict(path) for path in paths]

        for idx, path in enumerate(paths):
            path_baselines = np.append(all_path_baselines[idx], 0)
            # path_advantages = np.append(all_path_advantages[idx], 0)
            deltas = path["rewards"] + \
                     self.algo.discount * path_baselines[1:] - \
                     path_baselines[:-1]
            path["advantages"] = special.discount_cumsum(
                deltas, self.algo.discount * self.algo.gae_lambda)
            path["qvalues"] = path["advantages"] + path_baselines[:-1]
            path["returns"] = special.discount_cumsum(path["rewards"], self.algo.discount)
            # advantage_baselines.append(path_advantages[:-1])
            baselines.append(path_baselines[:-1])
            returns.append(path["returns"])

        ev = special.explained_variance_1d(
            np.concatenate(baselines),
            np.concatenate(returns)
        )

        old_advantages_to_fit = tensor_utils.concat_tensor_list([path["advantages"] for path in paths])
        logger.record_tabular("AdvantagesMean", old_advantages_to_fit.mean())

        if not self.algo.policy.recurrent:
            observations = tensor_utils.concat_tensor_list([path["observations"] for path in paths])
            actions = tensor_utils.concat_tensor_list([path["actions"] for path in paths])
            rewards = tensor_utils.concat_tensor_list([path["rewards"] for path in paths])
            returns = tensor_utils.concat_tensor_list([path["returns"] for path in paths])
            advantages = tensor_utils.concat_tensor_list([path["advantages"] for path in paths])
            qvalues = tensor_utils.concat_tensor_list([path["qvalues"] for path in paths])
            baselines_tensor = tensor_utils.concat_tensor_list(baselines)
            # baselines_advantage_tensor = tensor_utils.concat_tensor_list(advantage_baselines)
            env_infos = tensor_utils.concat_tensor_dict_list([path["env_infos"] for path in paths])
            agent_infos = tensor_utils.concat_tensor_dict_list([path["agent_infos"] for path in paths])
            etas = None

            if hasattr(self.algo, 'qprop') and self.algo.qprop:
                old_advantages = np.copy(advantages)
                old_advantages, _ = self.process_advantages(old_advantages)
                old_advantages_scale = np.abs(old_advantages).mean()
                logger.record_tabular("OldAdvantagesMSE", np.square(advantages).mean())
                logger.record_tabular("AbsLearnSignalOld", old_advantages_scale)
                logger.log("Qprop, subtracting control variate")
                advantages_bar = self.algo.get_control_variate(observations=observations, actions=actions)
                if self.algo.qprop_eta_option == 'ones':
                    etas = np.ones_like(advantages)
                elif self.algo.qprop_eta_option == 'adapt1': # conservative
                    etas = (advantages * advantages_bar) > 0
                    etas = etas.astype(advantages.dtype)
                    logger.log("Qprop, etas: %d 1s, %d 0s"%((etas == 1).sum(), (etas == 0).sum()))
                elif self.algo.qprop_eta_option == 'adapt2': # aggressive
                    etas = np.sign(advantages * advantages_bar)
                    etas = etas.astype(advantages.dtype)
                    logger.log("Qprop, etas: %d 1s, %d -1s"%((etas == 1).sum(), (etas == -1).sum()))
                else: raise NotImplementedError(self.algo.qprop_eta_option)
                """
                logger.record_tabular("Before Advantages MSE", np.mean(np.square(advantages)))
                advantages -= baselines_advantage_tensor
                logger.record_tabular("After Advantages MSE", np.mean(np.square(advantages)))
                """
                advantages -= etas * advantages_bar
                logger.record_tabular("NewAdvantagesMSE", np.square(advantages).mean())
                advantages, adv_std = self.process_advantages(advantages)
                if self.algo.qprop_unbias:
                    logger.log("Unbiasing Qprop estimator...")
                    etas /= adv_std
                advantages_scale = np.abs(advantages).mean()
                logger.record_tabular("AbsLearnSignalNew", advantages_scale)
            else:
                advantages, _ = self.process_advantages(advantages)
                advantages_scale = np.abs(advantages).mean()
                logger.record_tabular("AbsLearnSignal", advantages_scale)

            average_discounted_return = \
                np.mean([path["returns"][0] for path in paths])

            undiscounted_returns = [sum(path["rewards"]) for path in paths]

            ent = np.mean(self.algo.policy.distribution.entropy(agent_infos))

            samples_data = dict(
                observations=observations,
                actions=actions,
                rewards=rewards,
                returns=returns,
                advantages=advantages,
                qvalues=qvalues,
                env_infos=env_infos,
                agent_infos=agent_infos,
                paths=paths,
                baselines=baselines_tensor,
                etas=etas,
            )
        else:
            max_path_length = max([len(path["advantages"]) for path in paths])

            # make all paths the same length (pad extra advantages with 0)
            obs = [path["observations"] for path in paths]
            obs = tensor_utils.pad_tensor_n(obs, max_path_length)

            if self.algo.center_adv:
                raw_adv = np.concatenate([path["advantages"] for path in paths])
                adv_mean = np.mean(raw_adv)
                adv_std = np.std(raw_adv) + 1e-8
                adv = [(path["advantages"] - adv_mean) / adv_std for path in paths]
            else:
                adv = [path["advantages"] for path in paths]

            adv = np.asarray([tensor_utils.pad_tensor(a, max_path_length) for a in adv])

            actions = [path["actions"] for path in paths]
            actions = tensor_utils.pad_tensor_n(actions, max_path_length)

            rewards = [path["rewards"] for path in paths]
            rewards = tensor_utils.pad_tensor_n(rewards, max_path_length)

            returns = [path["returns"] for path in paths]
            returns = tensor_utils.pad_tensor_n(returns, max_path_length)

            agent_infos = [path["agent_infos"] for path in paths]
            agent_infos = tensor_utils.stack_tensor_dict_list(
                [tensor_utils.pad_tensor_dict(p, max_path_length) for p in agent_infos]
            )

            env_infos = [path["env_infos"] for path in paths]
            env_infos = tensor_utils.stack_tensor_dict_list(
                [tensor_utils.pad_tensor_dict(p, max_path_length) for p in env_infos]
            )

            valids = [np.ones_like(path["returns"]) for path in paths]
            valids = tensor_utils.pad_tensor_n(valids, max_path_length)

            baselines_tensor = tensor_utils.pad_tensor_n(baselines, max_path_length)

            average_discounted_return = \
                np.mean([path["returns"][0] for path in paths])

            undiscounted_returns = [sum(path["rewards"]) for path in paths]

            ent = np.sum(self.algo.policy.distribution.entropy(agent_infos) * valids) / np.sum(valids)

            samples_data = dict(
                observations=obs,
                actions=actions,
                advantages=adv,
                rewards=rewards,
                returns=returns,
                valids=valids,
                agent_infos=agent_infos,
                env_infos=env_infos,
                paths=paths,
                baselines=baselines_tensor,
            )

        logger.log("fitting baseline...")
        if hasattr(self.algo.baseline, 'fit_with_samples'):
            self.algo.baseline.fit_with_samples(paths, samples_data)
        else:
            self.algo.baseline.fit(paths)
        logger.log("fitted")

        # logger.log("evaluating fit baseline with another baseline...")
        # self.algo.extra_baseline.fit(old_advantages_to_fit, paths)
        # logger.log("fitted again")

        """
        logger.log("evaluating fit baseline with another baseline...")
        self.algo.extra_baseline.fit(baselines_tensor, paths)
        logger.log("fitted again")
        """


        logger.record_tabular('Iteration', itr)
        logger.record_tabular('AverageDiscountedReturn',
                              average_discounted_return)
        logger.record_tabular('AverageReturn', np.mean(undiscounted_returns))
        logger.record_tabular('ExplainedVariance', ev)
        logger.record_tabular('NumTrajs', len(paths))
        logger.record_tabular('Entropy', ent)
        logger.record_tabular('Perplexity', np.exp(ent))
        logger.record_tabular('StdReturn', np.std(undiscounted_returns))
        logger.record_tabular('MaxReturn', np.max(undiscounted_returns))
        logger.record_tabular('MinReturn', np.min(undiscounted_returns))

        return samples_data
