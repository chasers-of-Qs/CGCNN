from __future__ import print_function, division

import torch
import torch.nn as nn
from torch.autograd import Variable
import math
import numpy as np
import torch.nn.functional as F


class ConvLayer(nn.Module):
    """
    Convolutional operation on graphs
    """
    def __init__(self, atom_fea_len, nbr_fea_len,attn=True):
        """
        Initialize ConvLayer.

        Parameters
        ----------

        atom_fea_len: int
          Number of atom hidden features.
        nbr_fea_len: int
          Number of bond features.
        """
        super(ConvLayer, self).__init__()
        self.atom_fea_len = atom_fea_len
        self.nbr_fea_len = nbr_fea_len
        
        self.attn_type = attn
        self.attn = SelfAttention(input_size=2 * self.atom_fea_len + self.nbr_fea_len,
                                      attention_size=self.atom_fea_len, output_size=self.atom_fea_len*4)
       
        #self.fc_full = nn.Linear(2*self.atom_fea_len+self.nbr_fea_len,
                                 #2*self.atom_fea_len)
        self.fc_full = nn.Linear(256,
                                 2*self.atom_fea_len)
        self.sigmoid = nn.Sigmoid()
        self.softplus1 = nn.Softplus()
        self.bn1 = nn.BatchNorm1d(2*self.atom_fea_len)
        self.bn2 = nn.BatchNorm1d(self.atom_fea_len)
        self.softplus2 = nn.Softplus()

    def forward(self, atom_in_fea, nbr_fea, nbr_fea_idx):
        """
        Forward pass

        N: Total number of atoms in the batch
        M: Max number of neighbors

        Parameters
        ----------

        atom_in_fea: Variable(torch.Tensor) shape (N, atom_fea_len)
          Atom hidden features before convolution
        nbr_fea: Variable(torch.Tensor) shape (N, M, nbr_fea_len)
          Bond features of each atom's M neighbors
        nbr_fea_idx: torch.LongTensor shape (N, M)
          Indices of M neighbors of each atom

        Returns
        -------

        atom_out_fea: nn.Variable shape (N, atom_fea_len)
          Atom hidden features after convolution

        """
        # TODO will there be problems with the index zero padding?
        N, M = nbr_fea_idx.shape
        # convolution
        atom_nbr_fea = atom_in_fea[nbr_fea_idx, :]
        total_nbr_fea = torch.cat(
            [atom_in_fea.unsqueeze(1).expand(N, M, self.atom_fea_len),
             atom_nbr_fea, nbr_fea], dim=2)
        #print('total_nbr_fea',total_nbr_fea.shape)#torch.Size([10524, 12, 169])
        if self.attn_type:
            nbr_sumed = self.attn(total_nbr_fea)
            #print('nbr_sumed1',nbr_sumed.shape)#torch.Size([10524, 12, 256])
            '''
            nbr_sumed = torch.sum(nbr_sumed,dim=1)
            print('nbr_sumed2',nbr_sumed.shape)
            nbr_sumed = self.bn2(nbr_sumed)
        '''
        total_gated_fea = self.fc_full(nbr_sumed) 
        #total_gated_fea = self.fc_full(total_nbr_fea)    # SelfAttention(in=total_nbr_fea,attn_size=out,outsize=nbr_sumed)
        total_gated_fea = self.bn1(total_gated_fea.view(
                -1, self.atom_fea_len*2)).view(N, M, self.atom_fea_len*2)
        nbr_filter, nbr_core = total_gated_fea.chunk(2, dim=2)  # ????????????Eq. 5 ???????????????????????? (???????????????element-wise multiplication)  ????????????Self-attention
        nbr_filter = self.sigmoid(nbr_filter)
        nbr_core = self.softplus1(nbr_core)
        nbr_sumed = torch.sum(nbr_filter * nbr_core, dim=1)
        nbr_sumed = self.bn2(nbr_sumed)

        out = self.softplus2(atom_in_fea + nbr_sumed)  ## atom_in_fea????????????????????????  nbr_sumed??????GCN??????????????????
        return out


class CrystalGraphConvNet(nn.Module):
    """
    Create a crystal graph convolutional neural network for predicting total
    material properties.
    """
    def __init__(self, orig_atom_fea_len, nbr_fea_len,
                 atom_fea_len=64, n_conv=3, h_fea_len=128, n_h=1,
                 classification=False,attn=True):
        """
        Initialize CrystalGraphConvNet.

        Parameters
        ----------

        orig_atom_fea_len: int
          Number of atom features in the input.
        nbr_fea_len: int
          Number of bond features.
        atom_fea_len: int
          Number of hidden atom features in the convolutional layers
        n_conv: int
          Number of convolutional layers
        h_fea_len: int
          Number of hidden features after pooling
        n_h: int
          Number of hidden layers after pooling
        """
        super(CrystalGraphConvNet, self).__init__()
        self.classification = classification
        self.embedding = nn.Linear(orig_atom_fea_len, atom_fea_len)
        '''
        self.attn_type = attn
        self.attn = SelfAttention(input_size=2 * atom_fea_len + nbr_fea_len,
                                      attention_size=atom_fea_len, output_size=atom_fea_len)
        print('atom_fea_len',atom_fea_len)
        '''
        
        self.convs = nn.ModuleList([ConvLayer(atom_fea_len=atom_fea_len,
                                    nbr_fea_len=nbr_fea_len)
                                    for _ in range(n_conv)])
        self.conv_to_fc = nn.Linear(atom_fea_len, h_fea_len)
        self.conv_to_fc_softplus = nn.Softplus()
        if n_h > 1:
            self.fcs = nn.ModuleList([nn.Linear(h_fea_len, h_fea_len)
                                      for _ in range(n_h-1)])
            self.softpluses = nn.ModuleList([nn.Softplus()
                                             for _ in range(n_h-1)])
        if self.classification:
            self.fc_out = nn.Linear(h_fea_len, 2)
        else:
            self.fc_out = nn.Linear(h_fea_len, 1)
        if self.classification:
            self.logsoftmax = nn.LogSoftmax(dim=1)
            self.dropout = nn.Dropout()

    def forward(self, atom_fea, nbr_fea, nbr_fea_idx, crystal_atom_idx):
        """
        Forward pass

        N: Total number of atoms in the batch
        M: Max number of neighbors
        N0: Total number of crystals in the batch

        Parameters
        ----------

        atom_fea: Variable(torch.Tensor) shape (N, orig_atom_fea_len)
          Atom features from atom type
        nbr_fea: Variable(torch.Tensor) shape (N, M, nbr_fea_len)
          Bond features of each atom's M neighbors
        nbr_fea_idx: torch.LongTensor shape (N, M)
          Indices of M neighbors of each atom
        crystal_atom_idx: list of torch.LongTensor of length N0
          Mapping from the crystal idx to atom idx

        Returns
        -------

        prediction: nn.Variable shape (N, )
          Atom hidden features after convolution

        """
        atom_fea = self.embedding(atom_fea)
        #print('atom_fea1',atom_fea.shape)#torch.Size([10524, 64])
        '''
        if self.attn_type:
            atom_fea = self.attn(atom_fea)
        print('atom_fea2',atom_fea.shape)
        '''
        for conv_func in self.convs:
            atom_fea = conv_func(atom_fea, nbr_fea, nbr_fea_idx)
        crys_fea = self.pooling(atom_fea, crystal_atom_idx)
        crys_fea = self.conv_to_fc(self.conv_to_fc_softplus(crys_fea))
        crys_fea = self.conv_to_fc_softplus(crys_fea)
        if self.classification:
            crys_fea = self.dropout(crys_fea)
        if hasattr(self, 'fcs') and hasattr(self, 'softpluses'):
            for fc, softplus in zip(self.fcs, self.softpluses):
                crys_fea = softplus(fc(crys_fea))
        out = self.fc_out(crys_fea)
        if self.classification:
            out = self.logsoftmax(out)
        return out

    def pooling(self, atom_fea, crystal_atom_idx):
        """
        Pooling the atom features to crystal features

        N: Total number of atoms in the batch
        N0: Total number of crystals in the batch

        Parameters
        ----------

        atom_fea: Variable(torch.Tensor) shape (N, atom_fea_len)
          Atom feature vectors of the batch
        crystal_atom_idx: list of torch.LongTensor of length N0
          Mapping from the crystal idx to atom idx
        """
        assert sum([len(idx_map) for idx_map in crystal_atom_idx]) ==\
            atom_fea.data.shape[0]
        summed_fea = [torch.mean(atom_fea[idx_map], dim=0, keepdim=True)
                      for idx_map in crystal_atom_idx]
        return torch.cat(summed_fea, dim=0)


class SelfAttention(nn.Module):
    def __init__(self, input_size, attention_size, output_size, dropout=0.2):
        super(SelfAttention, self).__init__()

        self.attention_size = attention_size
        self.dropout = dropout
        self.K = nn.Linear(in_features=input_size, out_features=self.attention_size, bias=False)
        self.Q = nn.Linear(in_features=input_size, out_features=self.attention_size, bias=False)
        self.V = nn.Linear(in_features=input_size, out_features=self.attention_size, bias=False)
        self.output_layer = nn.Sequential(
            nn.Linear(in_features=self.attention_size, out_features=output_size, bias=False),
            # nn.Tanh(),
            nn.Dropout(self.dropout)
        )

    def forward(self, x):
        K = self.K(x)
        #print('K',K.shape)#torch.Size([10356, 12, 64])
        Q = self.Q(x).transpose(-1, -2)#torch.Size([10356, 64, 12])
        #print('Q',Q.shape)
        V = self.V(x).transpose(-1, -2)
        #print('V',V.shape)#torch.Size([10356, 64, 12])
        logits = torch.div(torch.matmul(K, Q), torch.tensor(np.sqrt(self.attention_size)))
        #print('logits',logits.shape)#torch.Size([10356, 12, 12])
        weight = F.softmax(logits, dim=-1)
        weight = weight.transpose(-1, -2)
        #print('weight',weight.shape)#torch.Size([10356, 12, 12])
        mid_step = torch.matmul(V, weight)
        #print('mid_step',mid_step.shape)#torch.Size([10356, 64, 12])
        # mid_step = torch.matmul(V, weight)
        attention = mid_step.transpose(-1, -2)
        #print ('attention',attention.shape)#torch.Size([10356, 12, 64])
        attention = self.output_layer(attention)
        #print (attention.shape)
        return attention

