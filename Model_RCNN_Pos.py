from MyPyTorchAPI.CNNUtils import *
import numpy as np
from MyPyTorchAPI.CustomActivation import *
from SE3Layer import GetTrans
from torch.autograd import Variable
from LSTMFC import LSTMFC
from CNNFC import CNNFC
from MyLSTM import MyLSTM

class Model_RCNN_Pos(nn.Module):
    def __init__(self, dsName='airsim'):
        super(Model_RCNN_Pos, self).__init__()
        input_channel = 2 if dsName.lower() == 'euroc' else 6
        input_size = (input_channel, 360, 720)
        seq1 = MySeqModel(input_size, [
            Conv2DBlock(input_channel, 64, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),
            Conv2DBlock(64, 128, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),
            Conv2DBlock(128, 256, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),
            Conv2DBlock(256, 512, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),
            Conv2DBlock(512, 1024, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),
            Conv2DBlock(1024, 6, kernel=3, stride=2, padding=1, atvn='prlu', bn = True, dropout=True),]
        )
        self.encoder = seq1.block
        NN_size = int(seq1.flattend_size)
        sigMax = np.sqrt(1)
        sigIncln = 0.1

        # CNNs
        # fc_du
        self.fc_du = CNNFC(NN_size, 3)
        self.fc_du_cov = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        # fc_dw
        self.fc_dw = CNNFC(NN_size, 3)
        self.fc_dw_cov = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        # fc_dtr_cov
        self.fc_dtr = GetTrans()
        self.fc_dtr_cov = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        self.init_w()

        # RNNs
        self.fc_du_rnn = CNNFC(NN_size, 3)
        self.fc_du_cov_rnn = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        self.fc_dw_rnn = CNNFC(NN_size, 3)
        self.fc_dw_cov_rnn = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        self.proc_dw_gt = nn.Sequential(nn.Linear(3, 64),
                                        nn.PReLU(),
                                        nn.BatchNorm1d(64),
                                        nn.Linear(64, 64),
                                        nn.PReLU())

        self.lstm = MyLSTM(NN_size+64, 2, NN_size)

        self.fc_dtr_cov_rnn = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))

        self.fc_pos_cov_rnn = nn.Sequential(CNNFC(NN_size, 6), Sigmoid(a=sigIncln, max=sigMax))


    def init_w(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.in_channels
                m.weight.data.normal_(0, 0.5 / np.sqrt(n))
                if m.bias is not None:
                    m.bias.data.zero_()

            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    # x1, x2: images: (1, delay, 3, 360, 720)
    # dw_gt: dRotation, (1, delay, 3)
    # pos_init: initial pos, (1, 3)
    def forward(self, x1, x2, dw_gt, pos_init):
        if x1.shape[0] != 1:
            print('error: batch size gotta be 1 per gpu')
            exit(1)
        # do CNN for the batch as series
        input = torch.cat((x1, x2), 2) #(1, delay, 6, 360, 720)
        x = self.encoder(input[0])
        x = x.view(x.size(0), -1) # (delay, 432)

        # process CNN output
        du_cnn = self.fc_du(x) #(delay, 3)
        dw_cnn = self.fc_dw(x) #(delay, 3)
        du_cnn_cov = self.fc_du_cov(x) #(delay, 6)
        dw_cnn_cov = self.fc_dw_cov(x) #(delay, 6)
        dtr_cnn = self.fc_dtr(du_cnn, dw_gt[0]) #(delay, 3)
        dtr_cnn_cov = self.fc_dtr_cov(x) #(delay, 6)

        # prep for RNN
        xSer = x.unsqueeze(0) #(1, delay, 432)

        # process dw_gt for RNN
        dw_gt_proc = self.proc_dw_gt(dw_gt[0])
        dw_gtSer = dw_gt_proc.unsqueeze(0)

        # LSTM
        lstm_input = torch.cat((xSer, dw_gtSer), dim=2)
        lstm_out = self.lstm(lstm_input)
        lstm_out = lstm_out.squeeze(0) #(delay, 432)

        # LSTM processed batch is ready
        du_rnn = self.fc_du_rnn(lstm_out) #(delay, 3)
        du_rnn_cov = self.fc_du_cov_rnn(lstm_out) #(delay, 6)

        dw_rnn = self.fc_dw_rnn(lstm_out) #(delay, 3)
        dw_rnn_cov = self.fc_dw_cov_rnn(lstm_out) #(delay, 6)

        dtr_rnn = self.fc_dtr(du_rnn, dw_gt[0]) #(delay, 3)
        dtr_rnn_cov = self.fc_dtr_cov_rnn(lstm_out)#(delay, 6)

        # calculate position through series
        dtr_rnn[0, :] = dtr_rnn[None, 0, :] + pos_init
        pos_rnn = torch.cumsum(dtr_rnn, dim=0) #(delay,3)
        pos_cov_rnn = self.fc_pos_cov_rnn(lstm_out)

        return du_cnn.unsqueeze(0), du_cnn_cov.unsqueeze(0), \
               dw_cnn.unsqueeze(0), dw_cnn_cov.unsqueeze(0), \
               dtr_cnn.unsqueeze(0), dtr_cnn_cov.unsqueeze(0), \
               du_rnn.unsqueeze(0), du_rnn_cov.unsqueeze(0), \
               dw_rnn.unsqueeze(0), dw_rnn_cov.unsqueeze(0), \
               dtr_rnn.unsqueeze(0), dtr_rnn_cov.unsqueeze(0), \
               pos_rnn.unsqueeze(0), pos_cov_rnn.unsqueeze(0)

if __name__ == '__main__':
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    m = nn.DataParallel(Model_RCNN_Pos(), device_ids=[0, 1]).to(device)
    img1 = torch.zeros((2, 10, 3, 360, 720), dtype=torch.float).cuda()
    img2 = img1
    dw_gt = torch.zeros((2, 10, 3), dtype=torch.float).cuda()
    pos_init = torch.zeros((2, 3), dtype=torch.float).cuda()
    du_cnn, du_cnn_cov, \
    dw_cnn, dw_cnn_cov, \
    dtr_cnn, dtr_cnn_cov, \
    du_rnn, du_rnn_cov, \
    dw_rnn, dw_rnn_cov, \
    dtr_rnn, dtr_rnn_cov, \
    pos= m.forward(img1, img2, dw_gt, pos_init)
    print(dtr_rnn.shape)
    print(pos.shape)


