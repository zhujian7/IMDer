import logging
import numpy as np
import torch
import torch.nn as nn
from torch import optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from tqdm import tqdm
from ..utils import MetricsTop, dict_to_str
logger = logging.getLogger('MMSA')

class IMDER():
    def __init__(self, args):
        self.args = args
        self.criterion = nn.L1Loss() if args.train_mode == 'regression' else nn.CrossEntropyLoss()
        self.metrics = MetricsTop(args.train_mode).getMetics(args.dataset_name)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def do_train(self, model, dataloader, return_epoch_results=False):
        optimizer = optim.Adam(model.parameters(), lr=self.args.learning_rate)
        scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, verbose=True, patience=self.args.patience)
        # initilize results
        epochs, best_epoch = 0, 0
        if return_epoch_results:
            epoch_results = {
                'train': [],
                'valid': [],
                'test': []
            }
        min_or_max = 'min' if self.args.KeyEval in ['Loss'] else 'max'
        best_valid = 1e8 if min_or_max == 'min' else 0

        # load pretrained
        origin_model = torch.load('pt/pretrained-{}.pth'.format(self.args.dataset_name), map_location=self.device)

        net_dict = model.state_dict()
        new_state_dict = {}
        for k, v in origin_model.items():
            k = k.replace('Model.', '')
            new_state_dict[k] = v
        net_dict.update(new_state_dict)
        model.load_state_dict(net_dict, strict=False)

        while True:
            epochs += 1
            # train
            y_pred, y_true = [], []
            losses = []
            model.train()
            train_loss = 0.0
            miss_one, miss_two = 0, 0  # num of missing one modal and missing two modal
            left_epochs = self.args.update_epochs
            with tqdm(dataloader['train']) as td:
                for batch_data in td:
                    if left_epochs == self.args.update_epochs:
                        optimizer.zero_grad()
                    left_epochs -= 1
                    vision = batch_data['vision'].to(self.args.device)
                    audio = batch_data['audio'].to(self.args.device)
                    text = batch_data['text'].to(self.args.device)
                    labels = batch_data['labels']['M'].to(self.args.device)
                    if self.args.train_mode == 'classification':
                        labels = labels.view(-1).long()
                    else:
                        labels = labels.view(-1, 1)
                    # forward
                    miss_2 = [0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0]
                    miss_1 = [0.1, 0.2, 0.3, 0.2, 0.1, 0.0, 0.0]
                    if miss_two / (np.round(len(dataloader['train']) / 10) * 10) < miss_2[int(self.args.mr*10-1)]:  # missing two modal
                        outputs = model(text, audio, vision, num_modal=1)
                        miss_two += 1
                    elif miss_one / (np.round(len(dataloader['train']) / 10) * 10) < miss_1[int(self.args.mr*10-1)]:  # missing one modal
                        outputs = model(text, audio, vision, num_modal=2)
                        miss_one += 1
                    else:  # no missing
                        outputs = model(text, audio, vision, num_modal=3)

                    # compute loss
                    task_loss = self.criterion(outputs['M'], labels)
                    loss_score_l = outputs['loss_score_l']
                    loss_score_v = outputs['loss_score_v']
                    loss_score_a = outputs['loss_score_a']
                    loss_rec = outputs['loss_rec']
                    combine_loss = task_loss + 0.1 * (loss_score_l + loss_score_v + loss_score_a + loss_rec)

                    # backward
                    combine_loss.backward()
                    if self.args.grad_clip != -1.0:
                        nn.utils.clip_grad_value_([param for param in model.parameters() if param.requires_grad],
                                                  self.args.grad_clip)
                    # store results
                    train_loss += combine_loss.item()
                    y_pred.append(outputs['M'].cpu())
                    y_true.append(labels.cpu())
                    if not left_epochs:
                        optimizer.step()
                        left_epochs = self.args.update_epochs
                if not left_epochs:
                    # update
                    optimizer.step()
            train_loss = train_loss / len(dataloader['train'])

            pred, true = torch.cat(y_pred), torch.cat(y_true)
            train_results = self.metrics(pred, true)
            logger.info(
                f"TRAIN-({self.args.model_name}) [{epochs - best_epoch}/{epochs}/{self.args.cur_seed}] "
                f">> loss: {round(train_loss, 4)} "
                f"{dict_to_str(train_results)}"
            )
            # validation
            val_results = self.do_test(model, dataloader['valid'], mode="VAL")
            test_results = self.do_test(model, dataloader['test'], mode="TEST")
            cur_valid = val_results[self.args.KeyEval]
            scheduler.step(val_results['Loss'])
            # save each epoch model
            model_save_path = 'pt/' + str(epochs) + '.pth'
            torch.save(model.state_dict(), model_save_path)
            # save best model
            isBetter = cur_valid <= (best_valid - 1e-6) if min_or_max == 'min' else cur_valid >= (best_valid + 1e-6)
            if isBetter:
                best_valid, best_epoch = cur_valid, epochs
                # save model
                torch.save(model.cpu().state_dict(), self.args.model_save_path)
                model.to(self.args.device)
            # epoch results
            if return_epoch_results:
                train_results["Loss"] = train_loss
                epoch_results['train'].append(train_results)
                epoch_results['valid'].append(val_results)
                test_results = self.do_test(model, dataloader['test'], mode="TEST")
                epoch_results['test'].append(test_results)
            # early stop
            if epochs - best_epoch >= self.args.early_stop:
                return epoch_results if return_epoch_results else None

    def do_test(self, model, dataloader, mode="VAL", return_sample_results=False):
        model.eval()
        y_pred, y_true = [], []
        miss_one, miss_two = 0, 0

        eval_loss = 0.0
        if return_sample_results:
            ids, sample_results = [], []
            all_labels = []
            features = {
                "Feature_t": [],
                "Feature_a": [],
                "Feature_v": [],
                "Feature_f": [],
            }
        with torch.no_grad():
            with tqdm(dataloader) as td:
                for batch_data in td:
                    vision = batch_data['vision'].to(self.args.device)
                    audio = batch_data['audio'].to(self.args.device)
                    text = batch_data['text'].to(self.args.device)
                    labels = batch_data['labels']['M'].to(self.args.device)
                    if self.args.train_mode == 'classification':
                        labels = labels.view(-1).long()
                    else:
                        labels = labels.view(-1, 1)
                    miss_2 = [0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0]
                    miss_1 = [0.1, 0.2, 0.3, 0.2, 0.1, 0.0, 0.0]
                    if miss_two / (np.round(len(dataloader) / 10) * 10) < miss_2[int(self.args.mr * 10 - 1)]:  # missing two modal
                        outputs = model(text, audio, vision, num_modal=1)
                        miss_two += 1
                    elif miss_one / (np.round(len(dataloader) / 10) * 10) < miss_1[int(self.args.mr * 10 - 1)]:  # missing one modal
                        outputs = model(text, audio, vision, num_modal=2)
                        miss_one += 1
                    else:  # no missing
                        outputs = model(text, audio, vision, num_modal=3)

                    if return_sample_results:
                        ids.extend(batch_data['id'])
                        for item in features.keys():
                            features[item].append(outputs[item].cpu().detach().numpy())
                        all_labels.extend(labels.cpu().detach().tolist())
                        preds = outputs["M"].cpu().detach().numpy()
                        sample_results.extend(preds.squeeze())

                    loss = self.criterion(outputs['M'], labels)
                    eval_loss += loss.item()
                    y_pred.append(outputs['M'].cpu())
                    y_true.append(labels.cpu())
        eval_loss = eval_loss / len(dataloader)
        pred, true = torch.cat(y_pred), torch.cat(y_true)

        eval_results = self.metrics(pred, true)
        eval_results["Loss"] = round(eval_loss, 4)
        logger.info(f"{mode}-({self.args.model_name}) >> {dict_to_str(eval_results)}")

        if return_sample_results:
            eval_results["Ids"] = ids
            eval_results["SResults"] = sample_results
            for k in features.keys():
                features[k] = np.concatenate(features[k], axis=0)
            eval_results['Features'] = features
            eval_results['Labels'] = all_labels

        return eval_results