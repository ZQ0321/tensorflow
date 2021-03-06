# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Abstractions for the head(s) of a model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc

import six

from tensorflow.python.estimator import model_fn
from tensorflow.python.estimator.canned import metric_keys
from tensorflow.python.estimator.canned import prediction_keys
from tensorflow.python.estimator.export import export_output
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import sparse_tensor
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import logging_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import metrics as metrics_lib
from tensorflow.python.ops import nn
from tensorflow.python.ops import string_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import weights_broadcast_ops
from tensorflow.python.ops.losses import losses
from tensorflow.python.platform import tf_logging as logging


class _Head(object):
  """Interface for the head/top of a model.

  Given logits (or output of a hidden layer), a Head knows how to compute
  predictions, loss, train_op, metrics and export outputs. It is meant to:

  1. Simplify writing model_fn and to make model_fn more configurable
  2. Support wide range of machine learning models. Since most heads can work
     with logits, they can support DNN, RNN, Wide, Wide&Deep,
     Global objectives, Gradient boosted trees and many other types
     of machine learning models.

  Common usage:
  Here is simplified model_fn to build a DNN regression model.
    ```python
    def _my_dnn_model_fn(features, labels, mode, params, config=None):
      # Optionally your callers can pass head to model_fn as a param.
      head = tf.contrib.learn.regression_head(...)
      input = tf.contrib.layers.input_from_feature_columns(features, ...)
      last_hidden_layer_out = tf.contrib.layers.stack(
          input, tf.contrib.layers.fully_connected, [1000, 500])
      logits = tf.contrib.layers.fully_connected(
          last_hidden_layer_out, head.logits_dimension, activation_fn=None)

      def _train_op_fn(loss):
        return optimizer.minimize(loss)

      return head.create_estimator_spec(
          features=features,
          labels=labels,
          mode=mode,
          logits=logits,
          train_op_fn=_train_op_fn)
    ```

  There are cases where computing and applying gradients can not be meaningfully
  captured with train_op_fn we support (for example, with sync optimizer). In
  such case, you can take the responsibility on your own. Here is a common
  use case,
    ```python
    estimator_spec = head.create_estimator_spec(
        features=features,
        labels=labels,
        mode=mode,
        logits=logits,
        train_op_fn=tf.contrib.learn.no_op_train_fn)
    if mode == model_fn.ModeKeys.TRAIN:
      optimizer = ...
      sync = tf.train.SyncReplicasOptimizer(opt=optimizer, ...)
      update_op = tf.contrib.layers.optimize_loss(optimizer=sync,
                                                  loss=estimator_spec.loss, ...)
      hooks = [sync.make_session_run_hook(is_chief)]
      ... upate train_op and hooks in EstimatorSpec and return
    ```
  """
  __metaclass__ = abc.ABCMeta

  @abc.abstractproperty
  def logits_dimension(self):
    """Size of the last dimension of the logits `Tensor`.

    Typically, logits is of shape `[batch_size, logits_dimension]`.

    Returns:
      The expected size of the `logits` tensor.
    """
    raise NotImplementedError('Calling an abstract method.')

  @abc.abstractmethod
  def create_estimator_spec(
      self, features, mode, logits, labels=None, train_op_fn=None):
    """Returns `EstimatorSpec` that a model_fn can return.

    Please note that,
    + Exactly one of `logits` and `logits_input` must be provided.
    + All args must be passed via name.

    Args:
      features: Input `dict` of `Tensor` objects.
      mode: Estimator's `ModeKeys`.
      logits: logits `Tensor` to be used by the head.
      labels: Labels `Tensor`, or `dict` of same.
      train_op_fn: Function that takes a scalar loss `Tensor` and returns an op
          to optimize the model with the loss. This is used in TRAIN mode and
          must not be None. None is allowed in other modes. If you want to
          optimize loss yourself you can pass `no_op_train_fn` and then use
          EstimatorSpec.loss to compute and apply gradients.

    Returns:
      `EstimatorSpec`.
    """
    raise NotImplementedError('Calling an abstract method.')


def _check_labels(labels, expected_labels_dimension):
  """Check labels type and shape."""
  with ops.name_scope(None, 'labels', (labels,)) as scope:
    labels = sparse_tensor.convert_to_tensor_or_sparse_tensor(labels)
    if isinstance(labels, sparse_tensor.SparseTensor):
      raise ValueError('SparseTensor labels are not supported.')
    labels_shape = array_ops.shape(labels)
    err_msg = 'labels shape must be [batch_size, {}]'.format(
        expected_labels_dimension)
    assert_rank = check_ops.assert_rank(labels, 2, message=err_msg)
    with ops.control_dependencies([assert_rank]):
      static_shape = labels.shape
      if static_shape is not None:
        dim1 = static_shape[1]
        if (dim1 is not None) and (dim1 != expected_labels_dimension):
          raise ValueError(
              'labels shape must be [batch_size, labels_dimension], got %s.' %
              (static_shape,))
      assert_dimension = check_ops.assert_equal(
          expected_labels_dimension, labels_shape[1], message=err_msg)
      with ops.control_dependencies([assert_dimension]):
        return array_ops.identity(labels, name=scope)


def _check_logits(logits, expected_logits_dimension):
  """Check logits type and shape."""
  with ops.name_scope(None, 'logits', (logits,)) as scope:
    logits = math_ops.to_float(logits)
    logits_shape = array_ops.shape(logits)
    assert_rank = check_ops.assert_rank(
        logits, 2, data=[logits_shape],
        message='logits shape must be [batch_size, logits_dimension]')
    with ops.control_dependencies([assert_rank]):
      static_shape = logits.shape
      if static_shape is not None:
        dim1 = static_shape[1]
        if (dim1 is not None) and (dim1 != expected_logits_dimension):
          raise ValueError(
              'logits shape must be [batch_size, logits_dimension], got %s.' %
              (static_shape,))
      assert_dimension = check_ops.assert_equal(
          expected_logits_dimension, logits_shape[1], data=[logits_shape],
          message='logits shape must be [batch_size, logits_dimension]')
      with ops.control_dependencies([assert_dimension]):
        return array_ops.identity(logits, name=scope)


def _indicator_labels_mean(labels, weights=None, name=None):
  with ops.name_scope(name, 'labels_mean', (labels, weights)) as scope:
    labels = math_ops.to_float(labels, name='labels')
    if weights is not None:
      weights = weights_broadcast_ops.broadcast_weights(weights, labels)
    return metrics_lib.mean(labels, weights=weights, name=scope)


def _accuracy_baseline(labels_mean):
  """Return accuracy baseline based on labels mean.

  This is the best the model could do by always predicting one class.

  Args:
    labels_mean: Tuple of value and update op.

  Returns:
    Tuple of value and update op.
  """
  with ops.name_scope(None, 'accuracy_baseline', labels_mean):
    value, update_op = labels_mean
    return (
        math_ops.maximum(value, 1. - value, name='value'),
        math_ops.maximum(update_op, 1 - update_op, name='update_op'))


def _predictions_mean(predictions, weights=None, name=None):
  with ops.name_scope(
      name, 'predictions_mean', (predictions, weights)) as scope:
    predictions = math_ops.to_float(predictions, name='predictions')
    if weights is not None:
      weights = weights_broadcast_ops.broadcast_weights(weights, predictions)
    return metrics_lib.mean(predictions, weights=weights, name=scope)


def _auc(labels, predictions, weights=None, curve='ROC', name=None):
  with ops.name_scope(name, 'auc', (predictions, labels, weights)) as scope:
    predictions = math_ops.to_float(predictions, name='predictions')
    if labels.dtype.base_dtype != dtypes.bool:
      logging.warning('Casting %s labels to bool.', labels.dtype)
      labels = math_ops.cast(labels, dtypes.bool)
    if weights is not None:
      weights = weights_broadcast_ops.broadcast_weights(weights, predictions)
    return metrics_lib.auc(
        labels=labels, predictions=predictions, weights=weights, curve=curve,
        name=scope)


def _accuracy_at_threshold(labels, predictions, weights, threshold, name=None):
  with ops.name_scope(
      name, 'accuracy_at_%s' % threshold,
      (predictions, labels, weights, threshold)) as scope:
    threshold_predictions = math_ops.to_float(
        math_ops.greater_equal(predictions, threshold))
    return metrics_lib.accuracy(
        labels=labels, predictions=threshold_predictions, weights=weights,
        name=scope)


def _precision_at_threshold(labels, predictions, weights, threshold, name=None):
  with ops.name_scope(
      name, 'precision_at_%s' % threshold,
      (predictions, labels, weights, threshold)) as scope:
    precision_tensor, update_op = metrics_lib.precision_at_thresholds(
        labels=labels, predictions=predictions, thresholds=(threshold,),
        weights=weights, name=scope)
    return array_ops.squeeze(precision_tensor), array_ops.squeeze(update_op)


def _recall_at_threshold(labels, predictions, weights, threshold, name=None):
  with ops.name_scope(
      name, 'recall_at_%s' % threshold,
      (predictions, labels, weights, threshold)) as scope:
    precision_tensor, update_op = metrics_lib.recall_at_thresholds(
        labels=labels, predictions=predictions, thresholds=(threshold,),
        weights=weights, name=scope)
    return array_ops.squeeze(precision_tensor), array_ops.squeeze(update_op)


# TODO(xiejw): Add class ids for eval metrics?
def _multi_class_head_with_softmax_cross_entropy_loss(
    n_classes, weight_column_name=None):
  """Creates a '_Head' for multi class classification.

  This head expects to be fed integer labels specifying the class index.

  Args:
    n_classes: Number of classes, must be greater than 2 (for 2 classes, use
      `_BinaryLogisticHeadWithSigmoidCrossEntropyLoss`).
    weight_column_name: A string defining feature column name representing
      weights. It is used to down weight or boost examples during training. It
      will be multiplied by the loss of the example.

  Returns:
    An instance of `_Head` for  multi class classification.

  Raises:
    ValueError: if `n_classes`, `metric_class_ids` or `label_keys` is invalid.
  """
  return _MultiClassHeadWithSoftmaxCrossEntropyLoss(
      n_classes, weight_column_name)


class _MultiClassHeadWithSoftmaxCrossEntropyLoss(_Head):
  """See `_multi_class_head_with_softmax_cross_entropy_loss`."""

  def __init__(self, n_classes, weight_column_name=None):
    if (n_classes is None) or (n_classes <= 2):
      raise ValueError('n_classes must be > 2: %s.' % n_classes)
    self._n_classes = n_classes
    self._weight_column_name = weight_column_name

  @property
  def logits_dimension(self):
    return self._n_classes

  def _eval_metric_ops(self, labels, probabilities, logits,
                       class_ids, weights, unweighted_loss):
    """Returns the Eval metric ops."""
    with ops.name_scope(
        None, 'metrics',
        (labels, probabilities, logits, class_ids, weights, unweighted_loss)):
      keys = metric_keys.MetricKeys
      metric_ops = {
          # Estimator already adds a metric for loss.
          # TODO(xiejw): Any other metrics?
          keys.LOSS_MEAN: metrics_lib.mean(
              unweighted_loss, weights=weights, name=keys.LOSS_MEAN),
          keys.ACCURACY: metrics_lib.accuracy(
              labels=labels, predictions=class_ids, weights=weights,
              name=keys.ACCURACY),
      }
    return metric_ops

  def create_estimator_spec(
      self, features, mode, logits, labels=None, train_op_fn=None):
    """See `Head`."""
    with variable_scope.variable_scope(
        None,
        default_name='multi_class_head',
        values=(tuple(six.itervalues(features)) + (labels, logits))):
      logits = _check_logits(logits, self.logits_dimension)

      # Predict.
      pred_keys = prediction_keys.PredictionKeys
      with ops.name_scope(None, 'predictions', (logits,)):
        # class_ids's shape is [batch_size]
        class_ids = math_ops.argmax(logits, 1, name=pred_keys.CLASSES)
        probabilities = nn.softmax(logits, name=pred_keys.PROBABILITIES)
        predictions = {
            pred_keys.LOGITS: logits,
            pred_keys.PROBABILITIES: probabilities,
            # Expand to [batch_size, 1]
            pred_keys.CLASSES: array_ops.expand_dims(class_ids, axis=(1,))
        }
      if mode == model_fn.ModeKeys.PREDICT:
        batch_size = array_ops.shape(probabilities)[0]
        output_classes = array_ops.tile(
            input=array_ops.expand_dims(input=math_ops.range(self._n_classes),
                                        axis=0),
            multiples=[batch_size, 1])
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.PREDICT,
            predictions=predictions,
            export_outputs={'': export_output.ClassificationOutput(
                scores=probabilities,
                # `ClassificationOutput` requires string classes.
                # TODO(xiejw): Support label_keys or label_column
                classes=string_ops.as_string(output_classes,
                                             name='str_classes'))})

      # Eval.
      labels = _check_labels(labels, 1)
      # Check that we got integer for classification.
      if not labels.dtype.is_integer:
        raise ValueError('Labels dtype should be integer '
                         'Instead got %s.' % labels.dtype)
      assert_less = check_ops.assert_less(
          labels, ops.convert_to_tensor(self._n_classes, dtype=labels.dtype),
          message='Label IDs must < n_classes')
      assert_greater = check_ops.assert_non_negative(
          labels, message='Label Ids must >= 0')
      with ops.control_dependencies((assert_less, assert_greater)):
        labels = array_ops.identity(labels)

      unweighted_loss = losses.sparse_softmax_cross_entropy(
          labels=labels, logits=logits, reduction=losses.Reduction.NONE)
      # Restore the squeezed dim, so unweighted_loss matches the weights shape.
      unweighted_loss = array_ops.expand_dims(unweighted_loss, axis=(1,))
      weights = (
          1. if (self._weight_column_name is None) else
          features[self._weight_column_name])
      weights = math_ops.to_float(weights, name='weights')
      training_loss = losses.compute_weighted_loss(
          unweighted_loss, weights=weights, reduction=losses.Reduction.SUM)
      if mode == model_fn.ModeKeys.EVAL:
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.EVAL,
            predictions=predictions,
            loss=training_loss,
            eval_metric_ops=self._eval_metric_ops(
                labels=labels,
                probabilities=probabilities,
                logits=logits,
                class_ids=class_ids,
                unweighted_loss=unweighted_loss,
                weights=weights))

      # Train.
      if train_op_fn is None:
        raise ValueError('train_op_fn can not be None.')
      logging_ops.scalar_summary(metric_keys.MetricKeys.LOSS, training_loss)
      logging_ops.scalar_summary(
          metric_keys.MetricKeys.LOSS_MEAN,
          losses.compute_weighted_loss(
              unweighted_loss, weights=weights,
              reduction=losses.Reduction.MEAN))
      return model_fn.EstimatorSpec(
          mode=model_fn.ModeKeys.TRAIN,
          predictions=predictions,
          loss=training_loss,
          train_op=train_op_fn(training_loss))


def _binary_logistic_head_with_sigmoid_cross_entropy_loss(
    weight_feature_key=None, thresholds=(0.5,)):
  """Creates a `Head` for single label binary classification.

  This head uses `sigmoid_cross_entropy_with_logits` loss.

  This head expects to be fed float labels of shape `(batch_size, 1)`.

  Args:
    weight_feature_key: A string defining feature column name representing
      weights. It is used to down weight or boost examples during training. It
      will be multiplied by the loss of the example.
    thresholds: Iterable of floats in the range `(0, 1)`. For binary
      classification metrics such as precision and recall, an eval metric is
      generated for each threshold value. This threshold is applied to the
      logistic values to determine the binary classification (i.e., above the
      threshold is `true`, below is `false`.

  Returns:
    An instance of `Head` for binary classification.

  Raises:
    ValueError: if `thresholds` contains a value outside of `(0, 1)`.
  """
  for threshold in thresholds:
    if (threshold <= 0.0) or (threshold >= 1.0):
      raise ValueError('thresholds not in (0, 1): %s.' % (thresholds,))
  return _BinaryLogisticHeadWithSigmoidCrossEntropyLoss(
      weight_feature_key=weight_feature_key, thresholds=thresholds)


class _BinaryLogisticHeadWithSigmoidCrossEntropyLoss(_Head):
  """See `_binary_logistic_head_with_sigmoid_cross_entropy_loss`."""

  def __init__(self, weight_feature_key=None, thresholds=None):
    self._weight_feature_key = weight_feature_key
    self._thresholds = tuple(thresholds)

  @property
  def logits_dimension(self):
    return 1

  def _eval_metric_ops(
      self, labels, logits, logistic, scores, classes, unweighted_loss,
      weights=None):
    with ops.name_scope(
        None, 'metrics',
        (labels, logits, logistic, scores, classes, unweighted_loss, weights)):
      keys = metric_keys.MetricKeys
      labels_mean = _indicator_labels_mean(
          labels=labels, weights=weights, name=keys.LABEL_MEAN)
      metric_ops = {
          # Estimator already adds a metric for loss.
          keys.LOSS_MEAN: metrics_lib.mean(
              unweighted_loss, weights=weights, name=keys.LOSS_MEAN),
          keys.ACCURACY: metrics_lib.accuracy(
              labels=labels, predictions=classes, weights=weights,
              name=keys.ACCURACY),
          keys.PREDICTION_MEAN: _predictions_mean(
              predictions=logistic, weights=weights, name=keys.PREDICTION_MEAN),
          keys.LABEL_MEAN: labels_mean,
          keys.ACCURACY_BASELINE: _accuracy_baseline(labels_mean),
          keys.AUC: _auc(
              labels=labels, predictions=logistic, weights=weights,
              name=keys.AUC),
          keys.AUC_PR: _auc(
              labels=labels, predictions=logistic, weights=weights, curve='PR',
              name=keys.AUC_PR)
      }
      for threshold in self._thresholds:
        accuracy_key = keys.ACCURACY_AT_THRESHOLD % threshold
        metric_ops[accuracy_key] = _accuracy_at_threshold(
            labels=labels, predictions=logistic, weights=weights,
            threshold=threshold, name=accuracy_key)
        # Precision for positive examples.
        precision_key = keys.PRECISION_AT_THRESHOLD % threshold
        metric_ops[precision_key] = _precision_at_threshold(
            labels=labels, predictions=logistic, weights=weights,
            threshold=threshold, name=precision_key)
        # Recall for positive examples.
        recall_key = keys.RECALL_AT_THRESHOLD % threshold
        metric_ops[recall_key] = _recall_at_threshold(
            labels=labels, predictions=logistic, weights=weights,
            threshold=threshold, name=recall_key)
      return metric_ops

  def create_estimator_spec(
      self, features, mode, logits, labels=None, train_op_fn=None):
    """See `Head`."""
    with variable_scope.variable_scope(
        None, default_name='binary_logistic_head',
        values=(tuple(six.itervalues(features)) + (labels, logits))):

      # Predict.
      pred_keys = prediction_keys.PredictionKeys
      logits = _check_logits(logits, self.logits_dimension)
      logistic = math_ops.sigmoid(logits, name=pred_keys.LOGISTIC)
      two_class_logits = array_ops.concat(
          (array_ops.zeros_like(logits), logits), 1, name='two_class_logits')
      scores = nn.softmax(two_class_logits, name=pred_keys.PROBABILITIES)
      classes = array_ops.reshape(
          math_ops.argmax(two_class_logits, axis=1), (-1, 1), name='classes')
      predictions = {
          pred_keys.LOGITS: logits,
          pred_keys.LOGISTIC: logistic,
          pred_keys.PROBABILITIES: scores,
          pred_keys.CLASSES: classes
      }
      if mode == model_fn.ModeKeys.PREDICT:
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.PREDICT,
            predictions=predictions,
            export_outputs={'': export_output.ClassificationOutput(
                scores=scores,
                # `ClassificationOutput` requires string classes.
                # TODO(ptucker): Support label_keys.
                classes=string_ops.as_string(classes, name='str_classes'))})

      # Eval.
      labels = _check_labels(math_ops.to_float(labels), self.logits_dimension)
      unweighted_loss = nn.sigmoid_cross_entropy_with_logits(
          labels=labels, logits=logits, name='loss')
      weights = (
          1. if (self._weight_feature_key is None) else
          features[self._weight_feature_key])
      weights = math_ops.to_float(weights, name='weights')
      training_loss = losses.compute_weighted_loss(
          unweighted_loss, weights=weights, reduction=losses.Reduction.SUM)
      if mode == model_fn.ModeKeys.EVAL:
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.EVAL,
            predictions=predictions,
            loss=training_loss,
            eval_metric_ops=self._eval_metric_ops(
                labels=labels,
                logits=logits,
                logistic=logistic,
                scores=scores,
                classes=classes,
                unweighted_loss=unweighted_loss,
                weights=weights))

      # Train.
      if train_op_fn is None:
        raise ValueError('train_op_fn can not be None.')
      logging_ops.scalar_summary(metric_keys.MetricKeys.LOSS, training_loss)
      logging_ops.scalar_summary(
          metric_keys.MetricKeys.LOSS_MEAN,
          losses.compute_weighted_loss(
              unweighted_loss, weights=weights,
              reduction=losses.Reduction.MEAN))
      return model_fn.EstimatorSpec(
          mode=model_fn.ModeKeys.TRAIN,
          predictions=predictions,
          loss=training_loss,
          train_op=train_op_fn(training_loss))


def _regression_head_with_mean_squared_error_loss(
    weight_feature_key=None,
    label_dimension=1):
  """Creates a `_Head` for regression using the mean squared loss.

  Args:
    weight_feature_key: A string defining feature column name representing
      weights. It is used to down weight or boost examples during training. It
      will be multiplied by the loss of the example.
    label_dimension: Number of regression labels per example. This is the size
      of the last dimension of the labels `Tensor` (typically, this has shape
      `[batch_size, label_dimension]`).

  Returns:
    An instance of `_Head` for linear regression.
  """
  return _RegressionHeadWithMeanSquaredErrorLoss(
      weight_feature_key=weight_feature_key,
      label_dimension=label_dimension)


class _RegressionHeadWithMeanSquaredErrorLoss(_Head):
  """`Head` for regression using the mean squared loss."""

  def __init__(self,
               label_dimension,
               weight_feature_key=None):
    """`Head` for regression.

    Args:
      label_dimension: Number of regression labels per example. This is the
        size of the last dimension of the labels `Tensor` (typically, this has
        shape `[batch_size, label_dimension]`).
      weight_feature_key: A string defining feature column name representing
        weights. It is used to down weight or boost examples during training. It
        will be multiplied by the loss of the example.

    Raises:
      ValueError: if `label_dimension` < 1.
    """
    if label_dimension < 1:
      raise ValueError('Invalid label_dimension %s.' % label_dimension)
    self._logits_dimension = label_dimension
    self._weight_feature_key = weight_feature_key

  @property
  def logits_dimension(self):
    return self._logits_dimension

  def create_estimator_spec(
      self, features, mode, logits, labels=None, train_op_fn=None):
    """See `Head`."""
    with variable_scope.variable_scope(
        None,
        default_name='regression_head',
        values=(tuple(six.itervalues(features)) + (labels, logits))):

      # Predict.
      logits = _check_logits(logits, self._logits_dimension)
      predictions = {prediction_keys.PredictionKeys.PREDICTIONS: logits}
      if mode == model_fn.ModeKeys.PREDICT:
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.PREDICT,
            predictions=predictions,
            export_outputs={'': export_output.RegressionOutput(value=logits)})

      # Eval.
      labels = _check_labels(math_ops.to_float(labels), self._logits_dimension)
      unweighted_loss = losses.mean_squared_error(
          labels=labels, predictions=logits, reduction=losses.Reduction.NONE)
      weights = (
          1. if (self._weight_feature_key is None) else
          features[self._weight_feature_key])
      weights = math_ops.to_float(weights, name='weights')
      training_loss = losses.compute_weighted_loss(
          unweighted_loss, weights=weights, reduction=losses.Reduction.SUM)
      if mode == model_fn.ModeKeys.EVAL:
        # Estimator already adds a metric for loss.
        eval_metric_ops = {
            metric_keys.MetricKeys.LOSS_MEAN: metrics_lib.mean(
                unweighted_loss, weights=weights)
        }
        return model_fn.EstimatorSpec(
            mode=model_fn.ModeKeys.EVAL,
            predictions=predictions,
            loss=training_loss,
            eval_metric_ops=eval_metric_ops)

      # Train.
      if train_op_fn is None:
        raise ValueError('train_op_fn can not be None.')
      logging_ops.scalar_summary(metric_keys.MetricKeys.LOSS, training_loss)
      logging_ops.scalar_summary(
          metric_keys.MetricKeys.LOSS_MEAN,
          losses.compute_weighted_loss(
              unweighted_loss, weights=weights,
              reduction=losses.Reduction.MEAN))
      return model_fn.EstimatorSpec(
          mode=model_fn.ModeKeys.TRAIN,
          predictions=predictions,
          loss=training_loss,
          train_op=train_op_fn(training_loss))
