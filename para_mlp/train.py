import copy
import gc
import logging
import statistics as stat
from typing import Any, Dict, List, Tuple

import numpy as np
from numpy.typing import NDArray
from sklearn.model_selection import KFold, ParameterGrid
from tqdm import tqdm

from para_mlp.config import Config
from para_mlp.data_structure import ModelParams
from para_mlp.model import RILRM
from para_mlp.pred import record_energy_prediction_accuracy
from para_mlp.utils import (
    average,
    make_high_energy_index,
    make_yids_for_structure_ids,
    rmse,
    round_to_4,
)

logger = logging.getLogger(__name__)


def make_param_grid(config: Config) -> Dict[str, Tuple[Any, ...]]:
    """Make parameter grid for grid search

    Args:
        config (Config): config to make machine learning model

    Returns:
        Dict[str, Tuple[Any]]: The parameter grid. All the possible values are stored
            for each key.
    """
    cutoff_radius_num = (
        int((config.cutoff_radius_max - config.cutoff_radius_min) / 2.0) + 1
    )
    cutoff_radius = tuple(
        radius
        for radius in np.linspace(
            config.cutoff_radius_min, config.cutoff_radius_max, cutoff_radius_num
        )
    )
    gaussian_params2_num = tuple(
        map(
            int,
            np.arange(
                config.gaussian_params2_num_min, config.gaussian_params2_num_max + 5, 5
            ),
        )
    )

    param_grid = {
        "cutoff_radius": cutoff_radius,
        "gaussian_params2_num": gaussian_params2_num,
        "alpha": config.alpha,
    }

    return param_grid


def arrange_model_from_hyper_params(hyper_params: dict, config: Config) -> RILRM:
    """Arrange RILRM from hyper parameters and Config

    Args:
        hyper_params (dict): Hyper parameters
        config (Config): Config to make machine learning model

    Returns:
        RILRM: Rotation Invariant type Linear Regression Model
    """
    # Keep hyper_params to store variable parameters
    model_params = ModelParams.from_dict(hyper_params)  # type: ignore

    model_params.composite_num = config.composite_num
    model_params.feature_type = config.feature_type
    model_params.gtinv_lmax = config.gtinv_lmax
    model_params.use_gtinv_sym = config.use_gtinv_sym
    model_params.use_force = config.use_force
    model_params.use_spin = config.use_spin
    model_params.gaussian_params2_flag = config.gaussian_params2_flag

    model_params.polynomial_model = config.polynomial_model
    model_params.polynomial_max_order = config.polynomial_max_order
    model_params.is_paramagnetic = config.is_paramagnetic
    model_params.delta_learning = config.delta_learning

    model_params.set_api_params()

    model = RILRM(model_params)

    return model


def cross_validate(
    config: Config,
    param_grid: Dict[str, Tuple],
    kfold_dataset: Dict[str, Any],
    high_energy_index_list: List[NDArray],
) -> RILRM:
    """Execute cross validation

    Args:
        config (Config): Config to make machine learning model
        param_grid (Dict[str, Tuple]): The parameter grid. All the possible values
            are stored for each key.
        kfold_dataset (Dict[str, Any]): store energy, force, and structure set
        high_energy_index_list (List[NDArray]): List of the column id
            for high energy structures

    Returns:
        RILRM: Model by selected cross validation
    """
    n_kfold_structure = len(kfold_dataset["structures"])
    index_matrix = np.zeros(n_kfold_structure)
    force_id_unit = (kfold_dataset["target"].shape[0] // n_kfold_structure) - 1
    n_atoms_in_structure = len(kfold_dataset["structures"][0].sites)

    retained_model_rmse = 1e10

    for hyper_params in tqdm(ParameterGrid(param_grid)):

        test_model = arrange_model_from_hyper_params(
            hyper_params=hyper_params,
            config=config,
        )

        test_model.make_feature(kfold_dataset["structures"], make_scaler=True)
        test_model.apply_weight(
            config.energy_weight,
            config.force_weight,
            config.high_energy_weights,
            high_energy_index_list,
            n_kfold_structure,
        )

        logger.debug(" Test model")
        logger.debug("    params : %s", hyper_params)
        logger.debug(f"    shape  : {test_model.x.shape}")
        logger.debug(f"    memory : {round(test_model.x.__sizeof__() / 1e9, 3)} (GB)")

        test_model_rmses, test_model_rmses_energy, test_model_rmses_force = [], [], []
        kf = KFold(n_splits=config.n_splits, shuffle=True, random_state=0)
        for train_index, valid_index in kf.split(index_matrix):
            yids_for_train = make_yids_for_structure_ids(
                train_index, n_kfold_structure, force_id_unit, config.use_force
            )
            yids_for_valid = make_yids_for_structure_ids(
                valid_index, n_kfold_structure, force_id_unit, config.use_force
            )
            test_model.train(
                yids_for_train["target"],
                kfold_dataset["target"],
            )

            y_predict = test_model.predict()

            test_model_rmses.append(
                rmse(
                    y_predict[yids_for_valid["target"]],
                    kfold_dataset["target"][yids_for_valid["target"]],
                )
            )
            test_model_rmses_energy.append(
                rmse(
                    y_predict[yids_for_valid["energy"]] / n_atoms_in_structure,
                    kfold_dataset["target"][yids_for_valid["energy"]]
                    / n_atoms_in_structure,
                )
                * 1e3
            )
            if config.use_force:
                test_model_rmses_force.append(
                    rmse(
                        y_predict[yids_for_valid["force"]],
                        kfold_dataset["target"][yids_for_valid["force"]],
                    )
                )

        test_model_rmse = average(test_model_rmses)
        rmse_std_dev = stat.stdev(test_model_rmses)
        test_model_rmses = [round_to_4(rmse) for rmse in test_model_rmses]
        logger.debug("    RMSE(target)         : %s", test_model_rmses)
        logger.debug(f"    RMSE(target, average): {test_model_rmse}")
        logger.debug(f"    RMSE(target, std_dev): {rmse_std_dev}")

        rmse_energy_average = average(test_model_rmses_energy)
        rmse_energy_std_dev = stat.stdev(test_model_rmses_energy)
        test_model_rmses_energy = [round_to_4(rmse) for rmse in test_model_rmses_energy]
        logger.debug("    RMSE(energy, meV/atom)         : %s", test_model_rmses_energy)
        logger.debug(f"    RMSE(energy, average, meV/atom): {rmse_energy_average}")
        logger.debug(f"    RMSE(energy, std_dev, meV/atom): {rmse_energy_std_dev}")

        if config.use_force:
            rmse_force_average = average(test_model_rmses_force)
            rmse_force_std_dev = stat.stdev(test_model_rmses_force)
            test_model_rmses_force = [
                round_to_4(rmse) for rmse in test_model_rmses_force
            ]
            logger.debug(
                "    RMSE(force, eV/ang)            : %s", test_model_rmses_force
            )
            logger.debug(f"    RMSE(force, average, eV/ang)   : {rmse_force_average}")
            logger.debug(f"    RMSE(force, std_dev, eV/ang)   : {rmse_force_std_dev}")

        # if config.metric == "energy":
        #     test_model_rmse = rmse_energy_average
        #     rmse_description = "energy, meV/atom"
        # elif config.use_force and (config.metric == "force"):
        #     test_model_rmse = rmse_force_average
        #     rmse_description = "force, eV/ang"
        # else:
        #     print("Cannot use RMSE(force) as metric because force data is not used.")
        #     sys.exit(1)

        if test_model_rmse < retained_model_rmse:
            retained_model_rmse = test_model_rmse
            retained_model = copy.deepcopy(test_model)
            retained_model_params = copy.deepcopy(hyper_params)
            # Free memory by assigning a new value
            retained_model.x = None

        logger.debug(" Retained model")
        logger.debug("    params      : %s", retained_model_params)
        logger.debug(f"    RMSE(target, average): {retained_model_rmse}")

    logger.info(" Best model")
    logger.info("    params: %s", retained_model_params)

    # Free memory by deleting unused object
    del test_model
    gc.collect()

    return retained_model


def train_and_eval(
    config: Config,
    kfold_dataset: Dict[str, Any],
    test_dataset: Dict[str, Any],
    yids_for_kfold: Dict[str, List[int]],
) -> RILRM:
    """Train candidate models and evaluate the best model's score

    Args:
        config (Config): Config to make machine learning model
        kfold_dataset (Dict[str, Any]): store energy, force, and structure set
        test_dataset (Dict[str, Any]): store energy, force, and structure set
        yids_for_kfold (Dict[str, List[int]]): The yids info about kfold target

    Returns:
        RILRM: trained model object
    """
    n_kfold_structure = len(kfold_dataset["structures"])
    force_id_unit = (kfold_dataset["target"].shape[0] // n_kfold_structure) - 1
    n_atoms_in_structure = len(kfold_dataset["structures"][0].sites)

    if config.energy_weight != 1.0:
        kfold_dataset["target"][:n_kfold_structure] *= config.energy_weight

    if config.force_weight != 1.0:
        kfold_dataset["target"][n_kfold_structure:] *= config.force_weight

    high_energy_index_list = None
    n_high_energy_structure = len(config.high_energy_weights)
    if (n_high_energy_structure != 1) or (config.high_energy_weights[0] != 1.0):
        n_structure = len(test_dataset["structures"]) + n_kfold_structure
        high_energy_index_list = [
            make_high_energy_index(
                high_energy_structure_file_id=i + 1,
                config=config,
                n_structure=n_structure,
                force_id_unit=force_id_unit,
                yids_for_kfold=yids_for_kfold,
            )
            for i in range(n_high_energy_structure)
        ]
        for i in range(n_high_energy_structure):
            kfold_dataset["target"][
                high_energy_index_list[i]
            ] *= config.high_energy_weights[i]

    # Cross validate, if necessary
    param_grid = make_param_grid(config)
    dont_cross_validate = all([len(val) == 1 for val in param_grid.values()])
    if dont_cross_validate:
        retained_model = arrange_model_from_hyper_params(
            hyper_params=ParameterGrid(param_grid)[0],
            config=config,
        )
    else:
        retained_model = cross_validate(
            config=config,
            param_grid=param_grid,
            kfold_dataset=kfold_dataset,
            high_energy_index_list=high_energy_index_list,
        )

    # Train retained model by using all the training data
    retained_model.make_feature(kfold_dataset["structures"], make_scaler=True)
    retained_model.apply_weight(
        config.energy_weight,
        config.force_weight,
        config.high_energy_weights,
        high_energy_index_list,
        n_kfold_structure,
    )
    train_index = [i for i in range(kfold_dataset["target"].shape[0])]
    retained_model.train(
        train_index,
        kfold_dataset["target"],
    )

    # Evaluate model's transferabilty for kfold data
    y_predict = retained_model.predict()

    energy_id_end = len(kfold_dataset["structures"])
    energy_predict = y_predict[:energy_id_end] / n_atoms_in_structure
    energy_expected = kfold_dataset["target"][:energy_id_end] / n_atoms_in_structure

    kfold_energy_filename = "/".join(
        [config.model_dir, "prediction", "kfold_energy.out"]
    )
    record_energy_prediction_accuracy(
        energy_predict, energy_expected, output_filename=kfold_energy_filename
    )

    model_score_energy = (
        rmse(
            energy_predict,
            energy_expected,
        )
        * 1e3
    )
    logger.info(f"    RMSE(train, energy, meV/atom): {model_score_energy}")
    if config.use_force:
        model_score_force = rmse(
            y_predict[energy_id_end:], kfold_dataset["target"][energy_id_end:]
        )
        logger.info(f"    RMSE(train, force, eV/ang): {model_score_force}")

    # Evaluate model's transferabilty for test data
    y_predict = retained_model.predict(test_dataset["structures"])

    energy_id_end = len(test_dataset["structures"])
    energy_predict = y_predict[:energy_id_end] / n_atoms_in_structure
    energy_expected = test_dataset["target"][:energy_id_end] / n_atoms_in_structure

    test_energy_filename = "/".join([config.model_dir, "prediction", "test_energy.out"])
    record_energy_prediction_accuracy(
        energy_predict, energy_expected, output_filename=test_energy_filename
    )

    model_score_energy = (
        rmse(
            energy_predict,
            energy_expected,
        )
        * 1e3
    )
    logger.info(f"    RMSE(test, energy, meV/atom): {model_score_energy}")
    if config.use_force:
        model_score_force = rmse(
            y_predict[energy_id_end:], test_dataset["target"][energy_id_end:]
        )
        logger.info(f"    RMSE(test, force, eV/ang): {model_score_force}")

    return retained_model
