import torch
from torch import nn
from torch.autograd import Variable
import numpy as np
import sklearn.cluster as cl


def acquisition(pool_loader, train_loader, model, opts):
    # creating loaders without shuffles
    pool_loader_noshuffle = torch.utils.data.DataLoader(
        pool_loader.dataset,
        batch_size=opts.batch_size,
        shuffle=False,
        num_workers=opts.n_threads,
        pin_memory=True)

    train_loader_noshuffle = torch.utils.data.DataLoader(
        train_loader.dataset,
        batch_size=opts.batch_size,
        shuffle=False,
        num_workers=opts.n_threads,
        pin_memory=True)

    # setting the number of the samples to be pooled
    if len(train_loader.dataset.indices) == 0: # initial acquisition
        print 'initial selection: n_pool = ', opts.init_train_size
        n_pool = opts.init_train_size
    else:
        print 'n_pool = ', opts.n_pool
        n_pool = opts.n_pool

    # calculating the score function (this is different from optimality) can be random or some uncertainty measure
    if opts.score_func == 'random':
        score = np.random.rand(len(pool_loader_noshuffle.dataset))
    else:
        raise ValueError('Invalid score function for data selection!')

    if opts.alpha == 0 or opts.optimality == 'none':
        opts.alpha = 0
        opts.optimality = 'none'

    # extracting labels and features (if necessary)
    if opts.clustering == 'labels' and opts.optimality == 'none':
        print 'extracting labels of the training dataset'
        train_features, train_labels = extract_features(train_loader_noshuffle, model, label_only=True)
        print 'extracting labels of the pooling dataset'
        pool_features, pool_labels = extract_features(pool_loader_noshuffle, model, label_only=True)
    elif not (opts.clustering == 'none' and opts.optimality == 'none'):
        print 'extracting features of the training dataset'
        train_features, train_labels = extract_features(train_loader_noshuffle, model, label_only=False)
        print 'extracting features of the pooling dataset'
        pool_features, pool_labels = extract_features(pool_loader_noshuffle, model, label_only=False)

    # clustering and selection
    print 'score function: ', opts.score_func
    print 'clustering method: ', opts.clustering
    print 'optimality function: ', opts.optimality
    if opts.optimality == 'none' and opts.clustering == 'none':  # only score func (no eig optimality or clustering)
        print 'selection based on only the score func (no eig optimality or clustering)'
        pooled_idx = np.argsort(score)[-n_pool:]
    else:
        # clustering
        if opts.clustering == 'none' or opts.n_clust == 1:
            print 'No clustering.'
            clust_pool = np.zeros(len(pool_loader_noshuffle.dataset))
            clust_train = np.zeros(len(train_loader_noshuffle.dataset))
            opts.n_clust = 1
            opts.n_pool_clust = n_pool
            print 'n_clust overwritten to', opts.n_clust, 'n_pool_clust overwritten to ', opts.n_pool_clust

        elif opts.clustering == 'labels':
            print 'Using labels as clusters.'
            clust_pool = np.array(pool_labels)
            clust_train = np.array(train_labels)
            opts.n_clust = len(set(pool_labels) | set(train_labels))
            opts.n_pool_clust = int(n_pool / opts.n_clust)
            print 'n_clust overwritten to', opts.n_clust, 'n_pool_clust overwritten to ', opts.n_pool_clust

        else:
            # clustering data in the feature space
            print 'Unsupervised clustering'
            clust_pool, clust_train = feature_clust(pool_features, train_features, opts.n_clust)

        # selection on clusters
        pooled_idx = clustered_acquisition(train_features, clust_train, pool_features, clust_pool, score, opts, n_pool)

    pooled_idx_set = set([pool_loader_noshuffle.dataset.indices[i] for i in pooled_idx])

    train_loader.dataset.indices = list(set(train_loader.dataset.indices) | pooled_idx_set)
    pool_loader.dataset.indices = list(set(pool_loader.dataset.indices) - pooled_idx_set)

    return



def extract_features(data_loader, model, label_only=False):
    feature_extractor = nn.Sequential(*list(model.module.children())[:-1])
    feature_extractor = feature_extractor.cuda()
    feature_extractor = nn.DataParallel(feature_extractor, device_ids=None)
    feature_extractor.eval()

    features = []
    labels = []
    for i, (inputs, l) in enumerate(data_loader):
        labels.extend(l.data.cpu().numpy())
        if not label_only:
            with torch.no_grad():
                inputs = Variable(inputs)
            batch_features = feature_extractor(inputs).data.view(inputs.size(0), -1)
            # print batch_features.shape
            # TODO: convert to numpy more efficiently
            features.extend(batch_features.cpu().numpy())
        if i % 100 == 0:
            print('[{0}/{1}]'.format(i + 1, len(data_loader)))
    return features, labels


def clustered_acquisition(f_train, clust_train, f_pool, clust_pool, score, args, n_pool):
    pooled_idx = []
    # optimal selction in each cluster
    for c in range(args.n_clust):
        idx_pool_c  = np.where(clust_pool == c)[0]
        idx_train_c = np.where(clust_train == c)[0]

        score_c = np.asarray([float(score[i]) for i in idx_pool_c])

        #n_pool_clust = np.minimum(n_pool, len(score_c))
        n_pool_clust = np.minimum(args.n_pool_clust, len(score_c))

        if args.optimality == 'none':
            # print 'alpha = 0 or optimality == none, selection based on just the score function: ', args.score_func
            pooled_idx_c = np.argsort(score_c)[-n_pool_clust:]
        else:
            f_pool_c = [f_pool[i][0] for i in idx_pool_c]
            f_train_c = [f_train[i][0] for i in idx_train_c]
            pooled_idx_c = optimal_acquisition(f_train_c, f_pool_c, score_c, n_pool_clust, args.alpha, type=args.optimality)

        pooled_idx.extend([int(idx_pool_c[i]) for i in pooled_idx_c])

    # kemoids on the selected samples
    # pooled_features = [f_pool[i][0] for i in pooled_idx]
    # dist_mat = pairwise_distances(pooled_features, metric='euclidean')
    # med_idx, _ = kmedoids.kMedoids(dist_mat, n_pool)
    #
    # return [int(pooled_idx[i]) for i in med_idx]

    # uncertainty selection on the selected samples
    pooled_score = [float(score[i]) for i in pooled_idx]
    sorted_idx = np.argsort(pooled_score)
    sorted_idx = sorted_idx[-n_pool:]
    return [int(pooled_idx[i]) for i in sorted_idx]


def feature_clust(f_pool, f_train, n_clust):
    N_pool = len(f_pool)
    data_f_pool = list(f_pool)
    data_f_train = list(f_train)

    data_f_pool.extend(data_f_train)

    spectral = cl.SpectralClustering(n_clusters=n_clust, eigen_solver='arpack', affinity="nearest_neighbors")
    spectral.fit(data_f_pool)
    labels = spectral.labels_

    # clusters = cl.k_means(data_f_pool, 10)  #Kmeans Clustering
    # labels = clusters[1]

    clust_pool = labels[0:N_pool]
    clust_train = labels[N_pool:]

    return clust_pool, clust_train

def optimal_acquisition(train, pool, score, n_pool, alpha, type):
    #pooled_idx = [int(cp.argmax(score))]
    pooled_idx = []
    while len(pooled_idx) < n_pool:
        if type == 'IPM':
            new_idx = IPM_add_sample(train, pool, pooled_idx)
        # elif type == 'MP':
        #     new_idx = MP_add_sample(train, pool, pooled_idx)
        else:
            new_idx = x_optimal_add_sample(train, pool, score, pooled_idx, alpha, type)

        pooled_idx.append(int(new_idx))
    return pooled_idx


def x_optimal_add_sample(train, pool, score, pooled_idx, alpha, type):
    candidate_samples = range(0, len(pool))         # all samples
    # candidate_samples = cp.argsort(score)[-100:]     # best samples based on score
    A_train = [np.ravel(t) for t in train]
    #if len(A_train) > 0:
    #    u, s, v = np.linalg.svd(A_train)
    #    A_train = [np.ravel(v[i]) for i in range(np.minimum(len(A_train), 5))]


    # calculating eig-based score
    eig_score = np.zeros(len(pool))
    for m in candidate_samples:

        if m in pooled_idx:
            continue

        set_idx = [int(idx) for idx in pooled_idx]
        set_idx.append(int(m))

        A = [np.ravel(pool[i]) for i in set_idx]
        A.extend(A_train)
        # A = [a/np.linalg.norm(a) for a in A]            # normalization
        A = np.asarray(A)

        eigs = np.linalg.eigvalsh(np.matmul(A, A.transpose()) + 0.5*np.eye(len(A)))

        if type == 'd_optimal':
            eig_score[m] = np.prod(eigs)                  # determinant
        elif type == 'e_optimal':
            eig_score[m] = np.min(eigs)                     # minimum eigen value      # determinant
        elif type == 'a_optimal':
            eig_score[m] = np.sum(eigs)                     # trace      # determinant
        elif type == 'inv_cond':
            eig_score[m] = np.min(eigs)/np.max(eigs)        # inverse condition number
        else:
            raise ValueError('Invalid optimality for data selection!')

    # finding the best sample
    eig_score /= np.ndarray.max(eig_score)
    objective = (1-alpha)*score + alpha*eig_score
    # objective = eig_score
    sorted_idx = np.argsort(objective)

    # avoiding duplicates
    i = 1
    while sorted_idx[-i] in pooled_idx:
        i += 1
    #print eig_score[sorted_idx[-i]]
    return sorted_idx[-i]


def IPM_add_sample(train, pool, pooled_idx):
    candidate_samples = range(0, len(pool))         # all samples
    # candidate_samples = cp.argsort(score)[-100:]     # best samples based on score
    set_idx = [int(idx) for idx in pooled_idx]

    A_train = [np.ravel(t) for t in train]
    A_train.extend([np.ravel(pool[i]) for i in set_idx])
    A_s_mat = np.array(A_train).transpose()
    if len(A_s_mat.shape) == 1:
        A_s_mat = A_s_mat.reshape((-1, 1))

    A_pool = [np.ravel(t) for t in pool]
    A_mat = np.array(A_pool).transpose()
    if len(A_mat.shape) == 1:
        A_mat = A_mat.reshape((-1, 1))

    #print A_s_mat.shape, A_mat.shape
    if len(A_s_mat) == 0:
        A_proj = A_mat
    else:
        Proj = np.matmul(A_s_mat, np.linalg.pinv(A_s_mat))
        A_proj = A_mat - np.matmul(Proj, A_mat)

    u, _, _ = np.linalg.svd(A_proj, full_matrices=False)
    first_eig_vec = u[0, :]
    # calculating MP score
    #print cp.linalg.norm(Res)
    correlation = np.zeros(len(pool))
    for m in candidate_samples:

        if m in pooled_idx:
            correlation[m] = 0
            continue

        correlation[m] = np.abs(np.inner(A_mat[:, m], first_eig_vec))

    # finding the best sample
    objective = correlation
    sorted_idx = np.argsort(objective)
    sorted_idx = np.flipud(sorted_idx)   # sort in decsending order

    # avoiding duplicates
    i = 0
    while sorted_idx[i] in pooled_idx:
        i += 1

    return sorted_idx[i]