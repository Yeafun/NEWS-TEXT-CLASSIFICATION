# 预训练脚本
import os
os.environ['TF_KERAS'] = '1'  # 必须使用tf.keras
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import tensorflow as tf
from bert4keras.backend import keras, K
from bert4keras.models import build_transformer_model
from bert4keras.optimizers import Adam
from bert4keras.optimizers import extend_with_gradient_accumulation
from bert4keras.optimizers import extend_with_layer_adaptation
from bert4keras.optimizers import extend_with_piecewise_linear_lr
from bert4keras.optimizers import extend_with_weight_decay
from keras.layers import Input, Lambda
from keras.models import Model

from data_utils import TrainingDatasetRoBERTa

# 语料路径和模型保存路径
model_saved_path = 'pre_models/bert_ecg_model.ckpt'
corpus_paths = [
    f'corpus_tfrecord/ecg_corpus.{i}.tfrecord' for i in range(10)
]

# 其他配置
sequence_length = 256
batch_size = 64
config_path = 'bert_config.json'
checkpoint_path = None  # 如果从零训练，就设为None
learning_rate = 0.00176
weight_decay_rate = 0.01
num_warmup_steps = 3125
num_train_steps = 125000
steps_per_epoch = 10000
grad_accum_steps = 16   # 大于1即表明使用梯度累积
epochs = num_train_steps * grad_accum_steps // steps_per_epoch
exclude_from_weight_decay = ['Norm', 'bias']
tpu_address = None      # 如果用多GPU跑，直接设为None
which_optimizer = 'lamb'    # adam 或 lamb，均自带weight decay
lr_schedule = {
    num_warmup_steps * grad_accum_steps: 1.0,
    num_train_steps * grad_accum_steps: 0.0,
}
floatx = K.floatx()

# 读取数据集，构建数据张量
dataset = TrainingDatasetRoBERTa.load_tfrecord(
    record_names=corpus_paths,
    sequence_length=sequence_length,
    batch_size=batch_size // grad_accum_steps,
)


def build_transformer_model_with_mlm():
    """带mlm的bert模型。"""
    bert = build_transformer_model(
        config_path, with_mlm='linear', return_keras_model=False
    )
    proba = bert.model.output

    # 辅助输入
    token_ids = Input(shape=(None,), dtype='int64', name='token_ids')   # 目标id
    is_masked = Input(shape=(None,), dtype=floatx, name='is_masked')    # mask标记

    def mlm_loss(inputs):
        """计算loss的函数，需要封装为一个层。"""
        y_true, y_pred, mask = inputs
        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred, from_logits=True
        )
        loss = K.sum(loss * mask) / (K.sum(mask) + K.epsilon())
        return loss

    def mlm_acc(inputs):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred, mask = inputs
        y_true = K.cast(y_true, floatx)
        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * mask) / (K.sum(mask) + K.epsilon())
        return acc

    mlm_loss = Lambda(mlm_loss, name='mlm_loss')([token_ids, proba, is_masked])
    mlm_acc = Lambda(mlm_acc, name='mlm_acc')([token_ids, proba, is_masked])

    train_model = Model(
        bert.model.inputs + [token_ids, is_masked], [mlm_loss, mlm_acc]
    )

    loss = {
        'mlm_loss': lambda y_true, y_pred: y_pred,
        'mlm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
    }

    return bert, train_model, loss


def build_transformer_model_for_pretraining():
    """构建训练模型，通用于TPU/GPU
    注意全程要用keras标准的层写法，一些比较灵活的“移花接木”式的
    写法可能会在TPU上训练失败。此外，要注意的是TPU并非支持所有
    tensorflow算子，尤其不支持动态（变长）算子，因此编写相应运算
    时要格外留意。
    """
    bert, train_model, loss = build_transformer_model_with_mlm()

    # 优化器
    optimizer = extend_with_weight_decay(Adam)
    if which_optimizer == 'lamb':
        optimizer = extend_with_layer_adaptation(optimizer)
    optimizer = extend_with_piecewise_linear_lr(optimizer)
    optimizer_params = {
        'learning_rate': learning_rate,
        'lr_schedule': lr_schedule,
        'weight_decay_rate': weight_decay_rate,
        'exclude_from_weight_decay': exclude_from_weight_decay,
        'bias_correction': False,
    }
    if grad_accum_steps > 1:
        optimizer = extend_with_gradient_accumulation(optimizer)
        optimizer_params['grad_accum_steps'] = grad_accum_steps
    optimizer = optimizer(**optimizer_params)

    # 模型定型
    train_model.compile(loss=loss, optimizer=optimizer)

    # 如果传入权重，则加载。注：须在此处加载，才保证不报错。
    if checkpoint_path is not None:
        bert.load_weights_from_checkpoint(checkpoint_path)

    return train_model,bert


if tpu_address is None:
    # 单机多卡模式（多机多卡也类似，但需要硬软件配合，请参考https://tf.wiki）
    strategy = tf.distribute.MirroredStrategy()
else:
    # TPU模式
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(
        tpu=tpu_address
    )
    tf.config.experimental_connect_to_host(resolver.master())
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.experimental.TPUStrategy(resolver)

with strategy.scope():
    train_model,bert = build_transformer_model_for_pretraining()
    train_model.summary()


class ModelCheckpoint(keras.callbacks.Callback):
    """自动保存最新模型。"""
    def on_epoch_end(self, epoch, logs=None):
        self.model.save_weights(model_saved_path, overwrite=True)
        bert.save_weights_as_checkpoint(filename='pre_models/bert_ecg_embedding_model_20210428.ckpt')

checkpoint = ModelCheckpoint()  # 保存模型
csv_logger = keras.callbacks.CSVLogger('training.log')  # 记录日志

# 模型训练
train_model.fit(
    dataset,
    steps_per_epoch=steps_per_epoch,
    epochs=epochs,
    callbacks=[checkpoint, csv_logger],
)

