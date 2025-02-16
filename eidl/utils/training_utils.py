import math
import os.path
import pickle

import numpy as np
import torch
from sklearn import metrics
from tqdm import tqdm
from torch import nn, autograd
import torch.nn.functional as F
import torch.nn.utils.rnn as rnn_utils
from eidl.utils.torch_utils import torch_wasserstein_loss
from eidl.viz.bad_gradient import is_bad_grad

def get_class_weight(labels, n_classes, smoothing_factor=0.1):
    """
    An example of one-hot encoded label array, the original labels are [0, 6]
    The corresponding cw is:
                 Count
    0 -> [1, 0]  100
    6 -> [0, 1]  200
    cw:  [3, 1.5]
    because pytorch treat [1, 0] as the first class and [0, 1] as the second class. However, the
    count for unique one-hot encoded label came out of np.unique is in the reverse order [0, 1] and [1, 0].
    the count needs to be reversed accordingly.

    TODO check when adding new classes
    @param convert_to_tensor:
    @param device:
    @return:
    """
    if len(labels.shape) == 2:  # if is onehot encoded
        unique_classes, counts = torch.unique(labels, return_counts=True, dim=0)
        class_frequencies = torch.zeros(n_classes).to(unique_classes.device)
        class_frequencies[torch.argmax(unique_classes, dim=1)] = counts.float()
        class_frequencies = torch.flip(class_frequencies, dims=[0])  # refer to docstring
    elif len(labels.shape) == 1:
        unique_classes, class_frequencies = torch.unique(labels, return_counts=True)
    else:
        raise ValueError("encoded labels should be either 1d or 2d array")
    if len(class_frequencies) == 1:  # when there is only one class in the dataset
        return None
    class_proportions = (class_frequencies + smoothing_factor) / len(labels)
    class_weights = 1 / class_proportions
    # class_weights[class_weights == torch.inf] = 0
    return class_weights  # reverse the class weights because

def run_one_epoch(mode, model: nn.Module, data_loader, device, n_classes, optimizer=None, criterion=nn.CrossEntropyLoss, pbar=None):
    if mode == 'train':
        model.train()
    else:
        model.eval()
    mini_batch_i = 0
    batch_losses = []
    num_correct_preds = 0
    y_all = None
    y_all_pred_postlogtis = None
    for batch_data in data_loader:
        x = batch_data['image']
        y = batch_data['y']

        if mode == 'train': optimizer.zero_grad()

        mini_batch_i += 1
        if pbar: pbar.update(1)

        y_pred = model(x.to(device))
        if isinstance(y_pred, tuple):
            y_pred, _ = y_pred
        y_pred_postlogits = F.softmax(y_pred, dim=1)
        # y_tensor = F.one_hot(y, num_classes=2).to(torch.float32).to(device)
        y_tensor = y.to(device).to(y_pred.dtype)

        class_weight = get_class_weight(y_tensor, n_classes=8)
        # loss = criterion(weight=class_weight)(y_tensor, y_pred)
        loss = criterion()(y_pred, y_tensor)

        if mode == 'train':
            loss.backward()
            optimizer.step()

        # add to y to compute auc
        y_all = np.concatenate([y_all, y.detach().cpu().numpy()]) if y_all is not None else y.detach().cpu().numpy()
        y_all_pred_postlogtis = np.concatenate([y_all_pred_postlogtis, y_pred_postlogits.detach().cpu().numpy()]) if y_all_pred_postlogtis is not None else y_pred_postlogits.detach().cpu().numpy()

        # measure accuracy
        num_correct_preds += torch.sum(torch.argmax(y_tensor, dim=1) == torch.argmax(y_pred, dim=1)).item()

        if pbar: pbar.set_description(f"{'Training' if mode == 'train' else 'Evaluating'} [{mini_batch_i}]: loss:{loss.item():.8f}")
        batch_losses.append(loss.item())
    acc = num_correct_preds / len(data_loader.dataset)
    auc = metrics.roc_auc_score(y_all, y_all_pred_postlogtis)
    return batch_losses, acc, auc


def train(model, optimizer: torch.optim.Optimizer, train_data_loader, val_data_loader, epochs, model_name, save_dir, n_classes, criterion=nn.CrossEntropyLoss):
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")

    train_losses = []
    train_accs = []
    val_losses = []
    val_accs = []
    val_aucs = []
    best_loss = np.inf
    training_histories = {}
    for epoch in range(epochs):
        pbar = tqdm(total=math.ceil(len(train_data_loader.dataset) / train_data_loader.batch_size),
                    desc='Training {}'.format(model_name))

        model.train()  # set the model in training model (dropout and batchnormal behaves differently in train vs. eval)
        train_batch_losses, train_acc, _ = run_one_epoch('train', model, train_data_loader, device, n_classes, optimizer, criterion, pbar)
        train_losses.append(np.mean(train_batch_losses))
        train_accs.append(train_acc)
        pbar.close()
        # scheduler.step()

        model.eval()
        with torch.no_grad():
            pbar = tqdm(total=math.ceil(len(val_data_loader.dataset) / val_data_loader.batch_size),  desc='Validating {}'.format(model_name))
            val_batch_losses, val_acc, val_auc = run_one_epoch('eval', model, val_data_loader, device, n_classes, optimizer,criterion, pbar)
            val_losses.append(np.mean(val_batch_losses))
            val_accs.append(val_acc)
            val_aucs.append(val_auc)
            pbar.close()
        lrl = [param_group['lr'] for param_group in optimizer.param_groups]
        lr = sum(lrl) / len(lrl)
        print(f"Epoch {epoch}: val auc = {val_aucs[-1]:.8f} train accuracy = {train_accs[-1]:.8f}, train loss={train_losses[-1]:.8f}; val accuracy = {val_accs[-1]:.8f}, val loss={val_losses[-1]:.8f}; LR={lr:.8f}")

        if val_losses[-1] < best_loss:
            torch.save(model.state_dict(), os.path.join(save_dir, model_name))
            print(
                'Best model loss improved from {} to {}, saved best model to {}'.format(best_loss, val_losses[-1],  save_dir))
            best_loss = val_losses[-1]

        # Save training histories after every epoch
        training_histories = {'loss_train': train_losses, 'acc_train': train_accs, 'loss_val': val_losses, 'acc_val': val_accs}
        pickle.dump(training_histories, open(os.path.join(save_dir, 'training_histories.pickle'), 'wb'))
    return training_histories

def run_validation(model: nn.Module, val_loader, device, dist=None, alpha=None, model_config_string='', criterion=nn.CrossEntropyLoss):
        model.eval()
        total_samples = 0
        total_loss = 0.0
        total_correct = 0

        pbar = tqdm(total=math.ceil(len(val_loader.dataset) / val_loader.batch_size),
                    desc=f'Validating {model_config_string}')
        pbar.update(mini_batch_i := 0)

        for batch in val_loader:
            mini_batch_i += 1
            pbar.update(1)

            image, label_encoded, label_onehot_encoded, fixation_sequence, aoi_heatmap, *_ = batch
            fixation_sequence_torch = torch.Tensor(rnn_utils.pad_sequence(fixation_sequence, batch_first=True))
            output, attention = model(image.to(device), fixation_sequence_torch.to(device))
            # pred = F.softmax(output, dim=1)

            aoi_heatmap = torch.flatten(aoi_heatmap, 1, 2)
            attention = torch.sum(attention, dim=1)  # summation across the heads
            attention /= torch.sum(attention, dim=1, keepdim=True)

            y_tensor = label_onehot_encoded.to(device)
            class_weight = get_class_weight(y_tensor, n_classes=2)
            classification_loss = criterion(weight=class_weight)(output, y_tensor)

            if dist is not None and alpha is not None:
                if dist == 'cross-entropy':
                    attention_loss = alpha * F.cross_entropy(attention, aoi_heatmap.to(device))
                elif dist == 'Wasserstein':
                    attention_loss = alpha * torch_wasserstein_loss(attention, aoi_heatmap.to(device))
                else:
                    raise NotImplementedError(f" Loss type {dist} is not implemented")
                loss = classification_loss + attention_loss
            else:
                loss = classification_loss

            _, predictions = torch.max(F.softmax(output, dim=1), 1)
            total_samples += (predictions.size(0))
            total_loss += loss.item() * len(batch[0])
            total_correct += torch.sum(predictions == label_encoded.to(device)).item()

        epoch_loss = total_loss / total_samples
        epoch_acc = (total_correct / total_samples)
        pbar.close()

        return epoch_loss, epoch_acc

def train_oct_model(model, model_config_string, train_loader, valid_loader, optimizer, results_dir, *, criterion=nn.CrossEntropyLoss, num_epochs=100, alpha=0.01, l2_weight=None, dist='cross-entropy'):
    def run_train(model: nn.Module, train_loader, optimizer, device):
        model.train()
        total_samples = 0
        total_loss = 0.0
        total_correct = 0
        mini_batch_i = 0
        pbar = tqdm(total=math.ceil(len(train_loader.dataset) / train_loader.batch_size), desc=f'Training {model_config_string}')
        pbar.update(mini_batch_i)

        grad_norms = []  # debug
        for batch in train_loader:
            mini_batch_i += 1
            pbar.update(1)

            image, label_encoded, label_onehot_encoded, fixation_sequence, aoi_heatmap, *_= batch
            fixation_sequence_torch = torch.Tensor(rnn_utils.pad_sequence(fixation_sequence, batch_first=True))
            output, attention = model(image.to(device), fixation_sequence=fixation_sequence_torch.to(device))
            # pred = F.softmax(output, dim=1)

            aoi_heatmap = torch.flatten(aoi_heatmap, 1, 2)
            attention = torch.sum(attention, dim=1)  # summation across the heads
            attention /= torch.sum(attention, dim=1, keepdim=True)  # normalize the attention output

            y_tensor = label_onehot_encoded.to(device)
            class_weight = get_class_weight(y_tensor, n_classes=2)
            classification_loss = criterion(weight=class_weight)(output, y_tensor)
            if dist == 'cross-entropy':
                attention_loss = alpha * F.cross_entropy(attention, aoi_heatmap.to(device))
            elif dist == 'Wasserstein':
                attention_loss = alpha * torch_wasserstein_loss(attention, aoi_heatmap.to(device))
            else:
                raise NotImplementedError(f" Loss type {dist} is not implemented")

            if l2_weight:
                l2_penalty = l2_weight * sum([(p ** 2).sum() for p in model.parameters()])
                loss = classification_loss + attention_loss + l2_penalty
            else:
                loss = classification_loss + attention_loss

            optimizer.zero_grad()

            with autograd.detect_anomaly():
                # get_dot = register_hooks(loss, name=f"epoch-{epoch}_minibatch-{mini_batch_i}")
                try:
                    loss.backward()
                except Exception as e:
                    print(f"Bad gradient encountered: {e}")
                finally:
                    pass
                    # dot = get_dot()
                    # dot.format = 'svg'
                    # dot.render(directory='bad gradients', view=True)

            grad_norms.append([torch.mean(param.grad.norm()).item() for _, param in model.named_parameters() if param.grad is not None])

            # nn.utils.clip_grad_norm_(model.parameters(), max_norm=2.0, norm_type=2)
            nn.utils.clip_grad_value_(model.parameters(), clip_value=1.0)
            bad_grads = {}
            for name, param in model.named_parameters():
                if param.grad is not None and is_bad_grad(param.grad):
                    print(f"Find nan in param.grad in module: {name}")
                    bad_grads[name] = param.grad

            optimizer.step()

            _, predictions = torch.max(F.softmax(output, dim=1), 1)
            total_samples += (predictions.size(0))
            total_loss += loss.item() * len(batch[0])
            total_correct += torch.sum(predictions == label_encoded.to(device)).item()
            pbar.set_description('Training [{}]: loss:{:.8f}, with classification loss {:.8f}, with attention loss {:.8f}'.format(mini_batch_i, loss.item(), classification_loss.item(), attention_loss.item()))

        epoch_loss = total_loss / total_samples
        epoch_acc = (total_correct / total_samples)
        pbar.close()
        return epoch_loss, epoch_acc


    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")

    best_acc = 0.0

    train_loss_list = []
    train_acc_list = []
    valid_loss_list = []
    valid_acc_list = []
    for epoch in range(num_epochs):
        print('epoch:{:d} / {:d}'.format(epoch, num_epochs))
        print('*' * 100)
        train_loss, train_acc = run_train(model, train_loader, optimizer, device=device)
        train_loss_list.append(train_loss)
        train_acc_list.append(train_acc)
        valid_loss, valid_acc = run_validation(model, valid_loader, dist=dist, device=device, model_config_string=model_config_string, criterion=criterion, alpha=alpha)
        valid_loss_list.append(valid_loss)
        valid_acc_list.append(valid_acc)
        print("training loss: {:.4f}, training acc: {:.4f}; validation loss {:.4f}, validation acc: {:.4f}".format(train_loss, train_acc, valid_loss, valid_acc))

        with open(os.path.join(results_dir, f'log_{model_config_string}.txt'), 'a+') as file:
            file.write('epoch:{:d} / {:d}\n'.format(epoch, num_epochs))
            file.write("training: {:.4f}, {:.4f}\n".format(train_loss, train_acc))
            file.write("validation: {:.4f}, {:.4f}\n".format(valid_loss, valid_acc))
        file.close()

        if valid_acc > best_acc:
            best_acc = valid_acc
            best_model = model
            torch.save(best_model, os.path.join(results_dir, f'best_{model_config_string}.pt'))
            torch.save(model.state_dict(), os.path.join(results_dir, f'best_{model_config_string}_statedict.pt'))

        if epoch >= 10 and len(set(train_acc_list[-10:])) == 1 and len(set(valid_acc_list[-10:])) == 1:
            break
    torch.save(model, os.path.join(results_dir, f'final_{model_config_string}.pt'))
    return train_loss_list, train_acc_list, valid_loss_list, valid_acc_list

def test_without_fixation(model, data_loader, device):
    total_samples = 0
    total_correct = 0
    model.train(False)
    predicted_attentions = []
    pbar = tqdm(total=math.ceil(len(data_loader.dataset) / data_loader.batch_size),
                desc=f'Testing without fixaiton')
    pbar.update(mini_batch_i := 0)

    with torch.no_grad():
        for batch in data_loader:
            mini_batch_i += 1
            pbar.update(1)

            image, label, label_encoded, fixation_sequence, aoi_heatmap = batch
            output, attention = model.test(image.to(device))
            pred = F.softmax(output, dim=1)

            predicted_attentions.append(attention.detach().cpu().numpy())
            _, predictions = torch.max(pred, 1)
            total_samples += (predictions.size(0))
            total_correct += torch.sum(predictions == label.to(device)).item()

    test_acc = (total_correct / total_samples)
    return test_acc