import hyperchamber as hc
import tensorflow as tf
import types
import uuid
import importlib
import hypergan
from hypergan.ops.tensorflow import layer_regularizers
from hypergan.ops.tensorflow.activations import lrelu, selu
from hypergan.ops.tensorflow.extended_ops import *

class TensorflowOps:
    def __init__(self, config={}, device="/gpu:0"):
        config = hc.Config(config)
        dtype = config.dtype or "float32"
        initializer = config.initializer or 'orthogonal'
        orthogonal_gain = config.orthogonal_gain or 1.0
        random_stddev = config.random_stddev or 0.02

        self.dtype = self.parse_dtype(dtype)
        self.scope_count = 0
        self.description = ''
        self.weights = []
        self.biases = []
        self.device = config.device
        self.initialized = False
        self._reuse = False
        self.config = config
        if initializer == 'orthogonal':
            self.initializer = self.orthogonal_initializer(orthogonal_gain)
        else:
            self.initializer = self.random_initializer(random_stddev)

    def assert_tensor(self, net):
        if type(net) != tf.Tensor and type(net) != tf.Variable:
            raise Exception("Expected a Tensor but received", net)

    def add_weights(self, weights):
        if not isinstance(weights, list):
            weights = [weights]
        self.weights += weights

    def variables(self):
        return self.biases + self.weights

    def random_initializer(self, stddev):
        def _build():
            return tf.random_normal_initializer(0, stddev, dtype=self.dtype)
        return _build

    def orthogonal_initializer(self, gain):
        def _build():
            return tf.orthogonal_initializer(gain)
        return _build

    def describe(self, description):
        self.description = description

    def reuse(self):
        self._reuse = True
        self.reuse_scope_count = 0

    def stop_reuse(self):
        self._reuse = False

    def generate_scope(self):
        if self._reuse:
            self.reuse_scope_count += 1
            return str(self.reuse_scope_count)
        self.scope_count += 1
        return str(self.scope_count)

    def generate_name(self):
        if self.description == "":
            return self.generate_scope()
        return self.description + "_" + self.generate_scope()

    def parse_dtype(self, dtype):
        if type(dtype) == Function:
            return dtype
        if dtype == 'float32':
            return tf.float32
        elif dtype == 'float16':
            return tf.float16
        else:
            raise Exception("dtype not defined: "+str(dtype))

    def get_weight(self, shape=None, name=None, initializer=None):
        if name == None:
            name = "w"
        if initializer == None:
            initializer = self.initializer()
        if shape is not None:
            weight = tf.get_variable(name, shape, dtype=self.dtype, initializer=initializer)
        else:
            weight = tf.get_variable(name, dtype=self.dtype, initializer=initializer)
        if not self._reuse:
            self.weights.append(weight)
        return weight

    def get_bias(self, shape, constant=0.0, name=None):
        if name == None:
            name='b'
        bias = tf.get_variable(name, shape, initializer=tf.constant_initializer(constant, dtype=self.dtype), dtype=self.dtype)
        if not self._reuse:
            self.biases.append(bias)
        return bias
    
    def parse_dtype(self, dtype):
        if dtype == 'float32':
            return tf.float32
        elif dtype == 'float16':
            return tf.float16
        else:
            raise Exception("dtype not defined: "+str(dtype))

    def cosine_conv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            w = self.get_weight([filter_h, filter_w, net.get_shape()[-1], output_dim])
            conv = tf.nn.conv2d(net, w, strides=[1, 1, 1, 1], padding='SAME')
            biases = self.get_bias([output_dim], 0.001)
            conv = tf.nn.bias_add(conv, biases)

            w_square = tf.square(w)
            #w_sum = tf.reduce_sum(w_square, [0,1,2])
            w_conv = tf.nn.conv2d(tf.ones_like(net), w_square, strides=[1, 1, 1, 1], padding='SAME')
            w_norm = tf.sqrt(w_conv + 1e-4)

            net_square = tf.square(net)
            w_ones = tf.ones_like(w)
            net_sum = tf.nn.conv2d(net_square, w_ones, strides=[1, 1, 1, 1], padding='SAME')
            net_norm = tf.sqrt(net_sum + 1e-4)

            return conv / (w_norm * net_norm)

    def weightnorm_conv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            w = self.get_weight([filter_h, filter_w, net.get_shape()[-1], output_dim])
            g = self.get_weight(name='g', shape=[1,output_dim])
            conv = tf.nn.conv2d(net, w, strides=[1, 1, 1, 1], padding='SAME')
            b = self.get_bias([output_dim], 0.001)

            w_square = tf.square(w)
            #w_sum = tf.reduce_sum(w_square, [0,1,2])
            w_conv = tf.nn.conv2d(tf.ones_like(net), w_square, strides=[1, 1, 1, 1], padding='SAME')
            w_norm = tf.sqrt(w_conv + 1e-4)

            return (conv*g+b) / (w_norm)

    #def weightnorm_conv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
    #    with tf.variable_scope(self.generate_name(), reuse=self._reuse):
    #        w = self.get_weight([filter_h, filter_w, net.get_shape()[-1], output_dim])
    #        g = self.get_weight(name='g', shape=[1,output_dim])
    #        b = self.get_bias([output_dim])

	#		# use weight normalization (Salimans & Kingma, 2016)
    #        W = tf.reshape(g,[1,1,1,output_dim])*tf.nn.l2_normalize(w,[0,1,2])

    #        # calculate convolutional layer output
    #        return tf.nn.bias_add(tf.nn.conv2d(net, W, [1, stride_h, stride_w, 1], padding='SAME'), b)

    def weightnorm_conv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            # modified from https://github.com/openai/weightnorm/blob/master/tensorflow/nn.py
            # data based initialization of parameters
            g = self.get_weight(name='g', shape=[1,1,1,output_dim])#, initializer=scale_init)
            b = self.get_bias(shape=[output_dim])#, initializer=-m_init*scale_init)
            shape = [filter_h, filter_w, int(net.get_shape()[-1]),output_dim]
            V = self.get_weight(name='v', shape=shape)
            V_norm = tf.nn.l2_normalize(V, [0,1,2])
            x_init = tf.nn.conv2d(net, V_norm, [1, stride_h, stride_w, 1], padding="SAME")
            x_init = tf.nn.bias_add(x_init, b)
            m_init, v_init = tf.nn.moments(x_init, [0,1,2])
            scale_init = 1.0/tf.sqrt(v_init + 1e-8)
            x_init = tf.reshape(scale_init,[1,1,1,output_dim])*(x_init-tf.reshape(m_init,[1,1,1,output_dim]))
            return g*x_init


    def weightnorm_deconv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            # modified from https://github.com/openai/weightnorm/blob/master/tensorflow/nn.py
            # data based initialization of parameters
            g = self.get_weight(name='g', shape=[1,1,1,output_dim])#, initializer=scale_init)
            b = self.get_bias(shape=[output_dim])#, initializer=-m_init*scale_init)
            shape = [filter_h, filter_w, output_dim, int(net.get_shape()[-1])]
            V = self.get_weight(name='v', shape=shape)
            V_norm = tf.nn.l2_normalize(V, [0,1,2])
            
            net_shape = self.shape(net)
            target_shape = [net_shape[0], net_shape[1]*stride_h, net_shape[2]*stride_w, output_dim]
            print(net, target_shape, V_norm)
            x_init = tf.nn.conv2d_transpose(net, V_norm, target_shape, [1, stride_h, stride_w, 1], padding="SAME")
            x_init = tf.nn.bias_add(x_init, b)
            m_init, v_init = tf.nn.moments(x_init, [0,1,2])
            scale_init = 1.0/tf.sqrt(v_init + 1e-8)
            x_init = tf.reshape(scale_init,[1,1,1,output_dim])*(x_init-tf.reshape(m_init,[1,1,1,output_dim]))
            return g*x_init


    def conv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        self.assert_tensor(net)

        print("CONF ", self.config)
        if self.config.layer_regularizer == 'cosine_norm':
            print("F S ", filter_w, stride_w)
            print("COSINE NORM")
            return self.cosine_conv2d(net, filter_w, filter_h, stride_w, stride_h, output_dim)
        if self.config.layer_regularizer == 'weight_norm':
            print("WEIGHT NORM")
            return self.weightnorm_conv2d(net, filter_w, filter_h, stride_w, stride_h, output_dim)

        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            w = self.get_weight([filter_h, filter_w, net.get_shape()[-1], output_dim])
            conv = tf.nn.conv2d(net, w, strides=[1, stride_h, stride_w, 1], padding='SAME')
            biases = self.get_bias([output_dim])
            conv = tf.nn.bias_add(conv, biases)
            return conv

    def deconv2d(self, net, filter_w, filter_h, stride_w, stride_h, output_dim):
        self.assert_tensor(net)
        initializer = self.initializer()
        shape = self.shape(net)
        if self.config.layer_regularizer == 'weight_norm':
            return self.weightnorm_deconv2d(net, filter_w, filter_h, stride_w, stride_h, output_dim)
        output_shape = [shape[0], shape[1]*stride_h, shape[2]*stride_w, output_dim]
        init_bias = 0.
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            # filter : [height, width, output_channels, in_channels]
            w = self.get_weight([filter_h, filter_w, output_dim, shape[3]])

            deconv = tf.nn.conv2d_transpose(net, w, output_shape=output_shape,
                                    strides=[1, stride_h, stride_w, 1])

            biases = self.get_bias([output_shape[-1]])
            deconv = tf.reshape(tf.nn.bias_add(deconv, biases), deconv.get_shape())

            return deconv

    def cosine_linear(self, net, output_dim):
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            w = self.get_weight([self.shape(net)[1], output_dim], name='cos_w')
            b = self.get_bias([output_dim], constant=0.001)
            w_norm = tf.sqrt(tf.reduce_sum(w**2, axis=0, keep_dims=True) + b ** 2)+0.000001
            x_norm = tf.sqrt(tf.reduce_sum(net**2, axis=1, keep_dims=True) + 0.000001)
            return (tf.matmul(net, w) + 0.001 * b) / w_norm / x_norm

    def linear(self, net, output_dim):
        if self.config.linear_type == 'cosine':
            return self.cosine_linear(net, output_dim)
        self.assert_tensor(net)
        initializer = self.initializer()
        shape = self.shape(net)
        with tf.variable_scope(self.generate_name(), reuse=self._reuse):
            w = self.get_weight([shape[1], output_dim])
            bias = self.get_bias([output_dim])
            return tf.matmul(net, w) + bias

    def reduce_linear(self):
        def _build(net, axis=1):
            return self.linear(net, 1)
        return _build


    def prelu(self):
        def _prelu(_x):
            orig_shape = self.shape(_x)
            _x = tf.reshape(_x, [orig_shape[0], -1])

            with tf.variable_scope(self.generate_name(), reuse=self._reuse):
                alphas = tf.get_variable('prelu', 
                          _x.get_shape()[-1],
                          initializer=tf.random_normal_initializer(mean=0.0,stddev=0.01),
                          dtype=tf.float32)
                pos = tf.nn.relu(_x)
                neg = alphas * (_x - abs(_x)) * 0.5

            self.add_weights(alphas)
            return tf.reshape(pos + neg, orig_shape)

        return _prelu

    def trelu(self):
        def _trelu(_x):
            activation = self.lookup(self.config.trelu_activation or 'relu')
            orig_shape = self.shape(_x)
            _x = tf.reshape(_x, [orig_shape[0], -1])

            with tf.variable_scope(self.generate_name(), reuse=self._reuse):
                alphas = tf.get_variable('trelu', 
                          _x.get_shape()[-1],
                          initializer=tf.random_normal_initializer(mean=0.0,stddev=0.01),
                          dtype=tf.float32)
                net = activation(_x - alphas) + alphas

            self.add_weights(alphas)
            return tf.reshape(net, orig_shape)

        return _trelu

    def frelu(self):
        def _frelu(_x):
            activation = self.lookup(self.config.frelu_activation or 'relu')
            orig_shape = self.shape(_x)
            _x = tf.reshape(_x, [orig_shape[0], -1])

            with tf.variable_scope(self.generate_name(), reuse=self._reuse):
                alphas = tf.get_variable('frelu', 
                          [1],
                          initializer=tf.random_normal_initializer(mean=0.0,stddev=0.01),
                          dtype=tf.float32)
                net = activation(_x) + alphas

            self.add_weights(alphas)
            return tf.reshape(net, orig_shape)

        return _frelu



    def reshape(self, net, shape):
        self.assert_tensor(net)
        return tf.reshape(net, shape)

    def concat(self, values=[], axis=0):
        return tf.concat(values=values, axis=axis)

    def resize_images(self, net, dims, op_type):
        self.assert_tensor(net)
        return tf.image.resize_images(net, dims, op_type)

    def slice(self, net, x, y):
        self.assert_tensor(net)
        return tf.slice(net, x, y)

    def shape(self, net):
        self.assert_tensor(net)
        return [(x._value or -1) for x in net.get_shape()]

    def add_n(self, net):
        return tf.add_n(net)

    def squash(self, net, reduce=tf.reduce_mean):
        """
        Takes any size tensor and reduces it to a single value using `reduce`.
        """
        while(sum(self.shape(net)) > 1):
            net = reduce(net)
            net = tf.squeeze(net)

        return net

    def lookup(self, symbol):
        if symbol == None:
            return None

        if type(symbol) == type([]):
            return [self.lookup(k) for k in symbol]

        if type(symbol) == type({}) or type(symbol) == hc.Config:
            return hc.Config({k: self.lookup(symbol[k]) for k in symbol.keys()})

        if type(symbol) != type(""):
            return symbol

        if symbol.startswith('function:'):
            return self.lookup_function(symbol)

        if symbol.startswith('class:'):
            return self.lookup_class(symbol)

        if symbol == 'tanh':
            return tf.nn.tanh
        if symbol == 'sigmoid':
            return tf.nn.sigmoid
        if symbol == 'cosine_norm':
            return "cosine_norm"
        if symbol == 'batch_norm':
            return layer_regularizers.batch_norm_1
        if symbol == 'layer_norm':
            return layer_regularizers.layer_norm_1
        if symbol == "crelu":
            return tf.nn.crelu
        if symbol == "prelu":
            return self.prelu()
        if symbol == "trelu":
            return self.trelu()
        if symbol == "selu":
            return selu
        if symbol == "frelu":
            return self.frelu()
        if symbol == "lrelu":
            return lrelu
        if symbol == "relu":
            return tf.nn.relu
        if symbol == 'square':
            return tf.square
        if symbol == 'reduce_mean':
            return tf.reduce_mean
        if symbol == 'reduce_min':
            return tf.reduce_min
        if symbol == 'reduce_sum':
            return tf.reduce_sum
        if symbol == 'reduce_logsumexp':
            return tf.reduce_logsumexp
        if symbol == 'reduce_linear':
            return self.reduce_linear()

        if symbol == 'l1_distance':
            return l1_distance
        if symbol == 'l2_distance':
            return l2_distance

        return symbol

    def lookup_function(self, name):
        namespaced_method = name.split(":")[1]
        method = namespaced_method.split(".")[-1]
        namespace = ".".join(namespaced_method.split(".")[0:-1])
        return getattr(importlib.import_module(namespace),method)

    def lookup_class(self, name):
        return self.lookup_function(name)

    def initialize_variables(self, session):
        with tf.device(self.device):
            if len(self.variables()) == 0:
                return
            init = tf.variables_initializer(self.variables())
            session.run(init)
            self.initialized = True

    def new_session(self, tfconfig):
        if tfconfig is None:
            tfconfig = tf.ConfigProto(allow_soft_placement=True)
            #tfconfig = tf.ConfigProto(log_device_placement=True)
            tfconfig.gpu_options.allow_growth=True

        with tf.device(self.device):
            return tf.Session(config=tfconfig)
