# Implementation of HRED model in PyTorch
# Paper : https://arxiv.org/abs/1507.04808
# python hred_pytorch.py <training_data> <dictionary>

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import json
import cPickle as pkl
import random
import sys
import time
import math
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import re

use_cuda = torch.cuda.is_available()

groups = []
word2id = {}
id2word = {}
EOS_token = None
SOS_token = None

# max sentence length
MAX_LENGTH = 100

class EncoderRNN(nn.Module):
    def __init__(self, input_size, hidden_size, n_layers=1):
        super(EncoderRNN, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(input_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size)

    def forward(self, input, hidden):
        embedded = self.embedding(input).view(1, 1, -1)
        output = embedded
        #h0,c0 = hidden
        for i in range(self.n_layers):
            output, hidden = self.gru(output, hidden)
        #hidden = (h1,c1)
        return output, hidden

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        #cell = Variable(torch.zeros(1,1,self.hidden_size))
        if use_cuda:
            return result.cuda()
        else:
            return result

class ContextRNN(nn.Module):
    def __init__(self, hidden_size, output_size, n_layers=1):
        super(ContextRNN, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size)

    def forward(self, input, hidden):
        #print input
        #output = self.embedding(input).view(1, 1, -1)
        output = input.view(1,1,-1)
        for i in range(self.n_layers):
            output, hidden = self.gru(output, hidden)
        return output, hidden

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if use_cuda:
            return result.cuda()
        else:
            return result


# for hred, decoder should also take the context vector and multiply that with the hidden state to form the new hidden
# state
# TODO: use beam search

class AttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, n_layers=1, dropout_p=0.1, max_length=MAX_LENGTH):
        super(AttnDecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout_p = dropout_p
        self.max_length = max_length

        self.embedding = nn.Embedding(self.output_size, self.hidden_size)
        self.attn = nn.Linear(self.hidden_size * 2, self.max_length)
        self.attn_combine = nn.Linear(self.hidden_size * 2, self.hidden_size)
        self.dropout = nn.Dropout(self.dropout_p)
        self.gru = nn.GRU(self.hidden_size, self.hidden_size)
        self.out = nn.Linear(self.hidden_size, self.output_size)

    def forward(self, input, hidden, encoder_output, encoder_outputs,context):
        embedded = self.embedding(input).view(1, 1, -1)
        embedded = self.dropout(embedded)

        attn_weights = F.softmax(
            self.attn(torch.cat((embedded[0], hidden[0]), 1)))
        attn_applied = torch.bmm(attn_weights.unsqueeze(0), # bmm - matmul
                                 encoder_outputs.unsqueeze(0))

        output = torch.cat((embedded[0], attn_applied[0]), 1)
        output = self.attn_combine(output).unsqueeze(0)

        # inputs are concatenation of previous output and context
        for i in range(self.n_layers):
            output = F.relu(output)
            output = torch.cat((output,context),0)
            output, hidden = self.gru(output, hidden)

        output = F.log_softmax(self.out(output[0])) # log softmax is done for NLL Criterion. We could use CrossEntropyLoss to avoid calculating this
        return output, hidden, attn_weights

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if use_cuda:
            return result.cuda()
        else:
            return result

teacher_forcing_ratio = 0.5

# for hred, train should take the context of the previous turn
# should return current loss as well as context representation

def train(input_variable, target_variable, encoder, decoder, context, context_hidden, encoder_optimizer, decoder_optimizer, criterion, last,max_length=MAX_LENGTH):
    global SOS_token
    encoder_hidden = encoder.initHidden()

    #encoder_optimizer.zero_grad() # pytorch accumulates gradients, so zero grad clears them up.
    #decoder_optimizer.zero_grad()

    input_length = input_variable.size()[0]
    target_length = target_variable.size()[0]

    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

    loss = 0

    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable[ei], encoder_hidden)
        encoder_outputs[ei] = encoder_output[0][0]

    decoder_input = Variable(torch.LongTensor([[SOS_token]]))
    decoder_input = decoder_input.cuda() if use_cuda else decoder_input

    decoder_hidden = encoder_hidden
    
    # calculate context
    context_output,context_hidden = context(encoder_output,context_hidden)

    use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

    if use_teacher_forcing:
        # Teacher forcing: Feed the target as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_output, encoder_outputs,context_hidden)
            loss += criterion(decoder_output[0], target_variable[di])
            decoder_input = target_variable[di]  # Teacher forcing

    else:
        # Without teacher forcing: use its own predictions as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_output, encoder_outputs,context_hidden)
            topv, topi = decoder_output.data.topk(1)
            ni = topi[0][0]

            decoder_input = Variable(torch.LongTensor([[ni]]))
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input

            # only calculate loss if its the last turn
            if last:
                loss += criterion(decoder_output[0], target_variable[di])
            if ni == EOS_token:
                break

    if last:
        loss.backward()

    #encoder_optimizer.step()
    #decoder_optimizer.step()

    if last:
        return loss.data[0] / target_length, context_hidden
    else:
        return context_hidden

def asMinutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return '%dm %ds' % (m, s)


def timeSince(since, percent):
    now = time.time()
    s = now - since
    es = s / (percent)
    rs = es - s
    return '%s (- %s)' % (asMinutes(s), asMinutes(rs))

def showPlot(points):
    plt.figure()
    fig, ax = plt.subplots()
    # this locator puts ticks at regular intervals
    loc = ticker.MultipleLocator(base=0.2)
    ax.yaxis.set_major_locator(loc)
    plt.plot(points)

def indexesFromSentence(word2id, sentence):
    return [word2id.get(word,word2id['<unk>']) for word in sentence.split(' ') if len(word) > 0]


def variableFromSentence(sentence=None,indexes=None):
    global EOS_token
    global word2id
    indexes = indexesFromSentence(word2id, sentence)
    indexes.append(EOS_token)
    result = Variable(torch.LongTensor(indexes).view(-1, 1),requires_grad=False)
    if use_cuda:
        return result.cuda()
    else:
        return result

def variablesFromPair(pair):
    input_variable = variableFromSentence(indexes=pair[0])
    target_variable = variableFromSentence(indexes=pair[1])
    return (input_variable, target_variable)

# return variables from group
def variablesFromGroup(group):
    variables = [variableFromSentence(sentence=p) for p in group]
    return variables

# training should proceed over each set of dialogs
# which should be in variable groups = [u1,u2,u3...un]
def trainIters(encoder, decoder, context,print_every=500, plot_every=100, evaluate_every=500, learning_rate=0.0001):
    global groups
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    encoder_optimizer = optim.Adam(encoder.parameters(), lr=learning_rate)
    decoder_optimizer = optim.Adam(decoder.parameters(), lr=learning_rate)
    context_optimizer = optim.Adam(context.parameters(), lr=learning_rate)
    #training_pairs = [variablesFromPair(random.choice(pairs))
    #                  for i in range(n_iters)]
    #training_groups = [variablesFromGroup(g) for g in groups]
    criterion = nn.NLLLoss()

    print "training started"
    iter = 0
    while True:
        iter +=1
        #training_pair = training_pairs[iter - 1]
        training_group = variablesFromGroup(random.choice(groups))
        #print len(training_group)
        context_hidden = context.initHidden()
        context_optimizer.zero_grad()
        encoder_optimizer.zero_grad() # pytorch accumulates gradients, so zero grad clears them up.
        decoder_optimizer.zero_grad()
        for i in range(0, len(training_group)-1):
            input_variable = training_group[i]
            target_variable = training_group[i+1]
            last = False
            if i + 1 == len(training_group) - 1:
                last = True

            if last:
                loss,context_hidden = train(input_variable, target_variable, encoder,
                         decoder, context, context_hidden, encoder_optimizer, decoder_optimizer, criterion, last)
                print_loss_total += loss
                plot_loss_total += loss
                encoder_optimizer.step()
                decoder_optimizer.step()
                context_optimizer.step()
            else:
                context_hidden = train(input_variable, target_variable, encoder,
                         decoder, context, context_hidden, encoder_optimizer, decoder_optimizer, criterion, last)

        if iter % print_every == 0:
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('steps %d loss %.4f' % (iter,print_loss_avg))

        if iter % (print_every * 3) == 0:
            # save models
            print "saving models"
            torch.save(encoder.state_dict(),'encoder_3.model')
            torch.save(decoder.state_dict(),'decoder_3.model')
            torch.save(context.state_dict(),'context_3.model')

        if iter % plot_every == 0:
            plot_loss_avg = plot_loss_total / plot_every
            plot_losses.append(plot_loss_avg)
            plot_loss_total = 0

        if iter % evaluate_every == 0:
            evaluateRandomly(encoder,decoder,context)

    #showPlot(plot_losses)

# TODO: evaluate with context
def evaluate(encoder, decoder, context, sentences, max_length=MAX_LENGTH):
    decoded_words = []
    decoder_attentions = torch.zeros(max_length, max_length)
    context_hidden = context.initHidden()
    
    for i,sentence in enumerate(sentences):
        last = False
        if i + 1 == len(sentences):
            last = True
        input_variable = variableFromSentence(sentence=sentence)
        input_length = input_variable.size()[0]
        encoder_hidden = encoder.initHidden()

        encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
        encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

        for ei in range(input_length):
            encoder_output, encoder_hidden = encoder(input_variable[ei],
                                                     encoder_hidden)
            encoder_outputs[ei] = encoder_outputs[ei] + encoder_output[0][0]

        decoder_input = Variable(torch.LongTensor([[SOS_token]]))  # SOS
        decoder_input = decoder_input.cuda() if use_cuda else decoder_input

        decoder_hidden = encoder_hidden

        # calculate context
        context_output,context_hidden = context(encoder_output,context_hidden)

        for di in range(max_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_output, encoder_outputs, context_hidden)
            decoder_attentions[di] = decoder_attention.data
            topv, topi = decoder_output.data.topk(1)
            ni = topi[0][0]
            if last:
                if ni == EOS_token:
                    decoded_words.append('<eos>')
                    break
                else:
                    decoded_words.append(id2word[ni])

            decoder_input = Variable(torch.LongTensor([[ni]]))
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input

    return decoded_words, decoder_attentions[:di + 1]

def evaluateRandomly(encoder, decoder, context, n=10):
    for i in range(n):
        group = random.choice(groups)
        for gr in group:
            print('>', gr)
        output_words, attentions = evaluate(encoder, decoder, context, group[:-1])
        output_sentence = ' '.join(output_words)
        print('<', output_sentence)
        print('')

if __name__=='__main__':
    # prepare data
    print "loading data"
    groups = []
    with open(sys.argv[1],'r') as fp:
        for line in fp:
            groups.append([re.sub('<[^>]+>', '',p.strip()).lstrip() 
                for p in line.replace('\n','').split('</s>') if len(p.strip()) > 0])
    dt = pkl.load(open(sys.argv[2],'r'))
    word2id = {d[0]:d[1] for d in dt}
    id2word = {d[1]:d[0] for d in dt}
    EOS_token = word2id['</s>']
    SOS_token = word2id['</d>']
    hidden_size = 300
    print len(word2id.keys())
    encoder1 = EncoderRNN(len(word2id.keys()), hidden_size)
    attn_decoder1 = AttnDecoderRNN(hidden_size, len(word2id.keys()),1, dropout_p=0.1)
    context1 = ContextRNN(hidden_size,len(word2id.keys()))

    if use_cuda:
        encoder1 = encoder1.cuda()
        attn_decoder1 = attn_decoder1.cuda()
        context1 = context1.cuda()

    trainIters(encoder1, attn_decoder1, context1, print_every=100, evaluate_every=600)



