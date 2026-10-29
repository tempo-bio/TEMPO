"""Nuclear training and cross-fitted helper generation in a TensorFlow worker."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile
import numpy as np


def helper_directory(dataset):
    dataset=Path(dataset)
    identity=hashlib.sha256((dataset/'dataset.json').read_bytes()).hexdigest()[:20]
    return dataset.parent/('nuclear-helpers-'+identity)


def profile_images(dataset, split, stems=None):
    from tempo_training import load_nuclear_partition
    images, labels = load_nuclear_partition(dataset, split)
    paths = sorted((Path(dataset)/'cells/images'/split).glob('*.png'))
    selected = [(image.astype(np.float32)/255, label) for path, image, label in zip(paths, images, labels)
                if stems is None or path.stem in stems]
    if not selected:
        raise ValueError('Nuclear training needs nonempty independent partitions.')
    return tuple(map(list, zip(*selected)))


def train_nuclear_model(dataset, output, epochs, batch, memory=None, *, validation_split='val',
                        train_stems=None, validation_stems=None, crossfit=True):
    from tempo_training import configure_tensorflow
    from tempo_model_catalog import MODEL_FILES
    configure_tensorflow(memory)
    import tensorflow as tf
    from stardist.models import Config2D, StarDist2D
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    info = json.loads((Path(dataset)/'dataset.json').read_text())
    tf.keras.utils.set_random_seed(info.get('seed',17))
    if crossfit and info.get('assisted', True):
        prepare_crossfit(dataset, output/'folds', epochs, batch, memory)
        tf.keras.utils.set_random_seed(info.get('seed',17))
    train_x, train_y = profile_images(dataset, 'train', train_stems)
    val_x, val_y = profile_images(dataset, validation_split, validation_stems)
    if not any(np.any(y > 0) for y in train_y):
        raise ValueError('Approve nuclear outlines before training StarDist; missing nuclei remain unknown.')
    if not any(np.any(y > 0) for y in val_y):
        raise ValueError('Nuclear validation needs reviewed positive instances.')
    patch = max(16, min(256, min(min(x.shape) for x in train_x+val_x))//16*16)
    config = Config2D(n_rays=32, n_channel_in=1, grid=(2,2), train_patch_size=(patch,patch),
                      train_batch_size=batch, train_epochs=epochs, train_steps_per_epoch=max(1,len(train_x)//batch))
    model = StarDist2D(config, name='model', basedir=str(output))
    from tempo_progress import report
    class Progress(tf.keras.callbacks.Callback):
        def on_epoch_end(self, epoch, logs=None):
            report('stardist', 'training_nuclei', epoch+1, epochs)
    model.prepare_for_training(); model.callbacks.append(Progress())
    model.train(train_x, train_y, validation_data=(val_x,val_y), epochs=epochs)
    model.load_weights('weights_best.h5')
    # Default thresholds are recorded, rather than optimized against incomplete truth.
    (output/'model'/'tempo_nuclei.json').write_text(json.dumps(dict(format='tempo-nuclear-model-v1',
        preprocessing=info['preprocessing'], seed=info.get('seed',17), grid=[2,2], partial_labels=True)))
    artifact = Path(shutil.make_archive(str(output/Path(MODEL_FILES['stardist']).stem), 'zip', root_dir=output, base_dir='model'))
    if crossfit and info.get('assisted', True):
        from tempo_nuclei import continuous_map
        # Confirmation inference is deferred until validation selection is frozen.
        for split in ('val',):
            if not list((Path(dataset)/'cells/images'/split).glob('*.png')):
                continue
            images, _ = profile_images(dataset, split)
            paths = sorted((Path(dataset)/'cells/images'/split).glob('*.png'))
            for path, image in zip(paths, images):
                save_map(dataset, path.stem, continuous_map(model,image), artifact, [])
        helper = helper_directory(dataset)/'helper_model.json'
        helper.parent.mkdir(exist_ok=True)
        helper.write_text(json.dumps(dict(path=str(artifact.resolve()), sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                         preprocessing=info['preprocessing'])))
    del model
    tf.keras.backend.clear_session()
    return str(artifact)


def save_map(dataset, stem, cue, model, excluded):
    folder = helper_directory(dataset); folder.mkdir(exist_ok=True)
    info = json.loads((Path(dataset)/'dataset.json').read_text())
    record = next(record for record in info['records'] if record['stem'] == stem)
    np.save(folder/(stem+'.npy'), cue.astype(np.float32), allow_pickle=False)
    (folder/(stem+'.json')).write_text(json.dumps(dict(source_sha256=record['source_sha256'],
        preprocessing=info['preprocessing'], model_path=str(Path(model).resolve()), model_sha256=hashlib.sha256(Path(model).read_bytes()).hexdigest(),
        excluded_preparations=excluded, preparation=record['preparation'])))


def prepare_crossfit(dataset, output, epochs, batch, memory):
    from tempo_training import unpack_stardist
    from tempo_nuclei import continuous_map
    from tempo_progress import report
    info = json.loads((Path(dataset)/'dataset.json').read_text())
    records = [record for record in info['records'] if record['split'] == 'train']
    unit='fields of view' if info.get('crossfit_unit')=='field' else 'biological preparations'
    if any(not isinstance(record.get('preparation'),str) or not record['preparation'] for record in records):
        raise ValueError('Assisted training is missing its verified cross-fit unit identifiers.')
    groups = sorted({record['preparation'] for record in records})
    if len(groups) < 4:
        raise ValueError('Assisted Cellpose needs at least four training '+unit+' for cross-fitting and internal validation.')
    positive={record['preparation'] for record in records if np.any(np.load(
        Path(dataset)/'nuclei/train'/(record['stem']+'.npy'),allow_pickle=False)>0)}
    if len(positive)<3:
        raise ValueError('Cross-fitting needs reviewed nuclei in at least three training units.')
    folds = [sorted(positive)[index::3] for index in range(3)]
    for group in sorted(set(groups)-positive):
        min(folds,key=len).append(group)
    for index, excluded in enumerate(folds):
        report('stardist', 'helper_fold_'+str(index+1), index, 3)
        remaining = [group for group in groups if group not in excluded]
        validation = {next(group for group in reversed(remaining) if group in positive)}
        training = set(remaining)-validation
        train_stems={r['stem'] for r in records if r['preparation'] in training}
        val_stems={r['stem'] for r in records if r['preparation'] in validation}
        fold=Path(output)/str(index);fold.mkdir(parents=True,exist_ok=True)
        saved=fold/'fold_result.json'
        identity=dict(dataset_sha256=hashlib.sha256((Path(dataset)/'dataset.json').read_bytes()).hexdigest(),
                      epochs=epochs,batch=batch,train=sorted(train_stems),validation=sorted(val_stems))
        cached=json.loads(saved.read_text()) if saved.is_file() else {}
        if cached:
            if cached['identity']!=identity or hashlib.sha256(Path(cached['artifact']).read_bytes()).hexdigest()!=cached['sha256']:
                raise ValueError('A completed helper fold differs from its frozen training data or weights.')
            artifact=cached['artifact']
        else:
            artifact = train_nuclear_model(dataset, fold, epochs, batch, memory,
                validation_split='train',train_stems=train_stems,validation_stems=val_stems,crossfit=False)
            from tempo_annotation_store import atomic_bytes
            atomic_bytes(saved,json.dumps(dict(identity=identity,artifact=artifact,
                sha256=hashlib.sha256(Path(artifact).read_bytes()).hexdigest())).encode())
        with tempfile.TemporaryDirectory() as folder:
            model = unpack_stardist(artifact, folder)
            paths = sorted((Path(dataset)/'cells/images/train').glob('*.png'))
            images, _ = profile_images(dataset, 'train')
            selected = {r['stem'] for r in records if r['preparation'] in excluded}
            for path, image in zip(paths, images):
                if path.stem in selected:
                    save_map(dataset,path.stem,continuous_map(model,image),artifact,excluded)
            del model
        import tensorflow as tf
        tf.keras.backend.clear_session()


def load_helpers(dataset, split):
    info = json.loads((Path(dataset)/'dataset.json').read_text())
    arrays = []
    for path in sorted((Path(dataset)/'cells/images'/split).glob('*.png')):
        record = next(r for r in info['records'] if r['stem'] == path.stem)
        metadata = json.loads((helper_directory(dataset)/(path.stem+'.json')).read_text())
        if metadata['source_sha256'] != record['source_sha256'] or metadata['preprocessing'] != info['preprocessing']:
            raise ValueError('Nuclear helper cache is stale; regenerate it.')
        if hashlib.sha256(Path(metadata['model_path']).read_bytes()).hexdigest() != metadata['model_sha256']:
            raise ValueError('Helper weights changed; regenerate cached maps.')
        if split == 'train' and record['preparation'] not in metadata['excluded_preparations']:
            raise ValueError('A training helper was not predicted out of fold.')
        arrays.append(np.load(helper_directory(dataset)/(path.stem+'.npy'),allow_pickle=False))
    return arrays


def benchmark_nuclei(path, dataset, split):
    from tempo_training import unpack_stardist, instance_counts
    images, labels = profile_images(dataset, split)
    matched = total = outside = inside_unmatched = 0
    with tempfile.TemporaryDirectory() as folder:
        model = unpack_stardist(path, folder)
        for index,(image, truth) in enumerate(zip(images, labels),1):
            print(f'stardist: predicting evaluation image {index}/{len(images)}.',flush=True)
            prediction = model.predict_instances(image)[0]
            print(f'stardist: matching evaluation image {index}/{len(images)}.',flush=True)
            a, _, c = instance_counts(np.maximum(truth,0), prediction)
            print(f'stardist: image {index}/{len(images)} evaluated; {a} matched, {c} missed annotated nuclei.',flush=True)
            matched += a; total += a+c
            for value in np.unique(prediction[prediction>0]):
                pixels = prediction == value
                if np.count_nonzero(pixels & (truth == 0)) / max(1,pixels.sum()) >= .5:
                    outside += 1
                elif not np.any(pixels & (truth>0)):
                    inside_unmatched += 1
    return dict(metric='annotated_nuclear_recall_IoU50',score=matched/max(1,total),
                annotated_nuclei=total, outside_false_positives=outside, unreviewed_inside_predictions=inside_unmatched)


def predict_maps_isolated(model_path, images):
    """Terminate TensorFlow before a Cellpose network takes GPU ownership."""
    import subprocess
    import sys
    import os
    with tempfile.TemporaryDirectory(prefix='tempo-nuclear-input-') as folder:
        folder = Path(folder)
        np.savez(folder/'images.npz', **{str(index):image for index,image in enumerate(images)})
        inputs = [str(model_path), str(folder/'images.npz'), str(folder/'maps.npz')]
        command = ([sys.executable, '--predict-nuclei', *inputs] if getattr(sys, 'frozen', False)
                   else [sys.executable, str(Path(__file__).resolve()), '--predict', *inputs])
        result = subprocess.run(command, capture_output=True, text=True,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        if result.returncode:
            from tempo_credentials import redact_credentials
            detail = ''
            sidecar = Path(str(folder/'maps.npz')+'.error.json')
            try:
                if sidecar.stat().st_size <= 16384:
                    failure = json.loads(sidecar.read_text(encoding='utf-8'))
                    if isinstance(failure, dict) and isinstance(failure.get('message'), str):
                        detail = redact_credentials(failure['message'])[-2000:]
            except (OSError, ValueError, TypeError):
                pass
            detail = detail or redact_credentials((result.stderr or '').strip())[-2000:] or f'worker exited with code {result.returncode}'
            raise RuntimeError('Nuclear helper inference failed: '+detail)
        if not (folder/'maps.npz').is_file():
            raise RuntimeError('Nuclear helper exited without producing prediction maps.')
        try:
            with np.load(folder/'maps.npz',allow_pickle=False) as maps:
                expected = {str(index) for index in range(len(images))}
                if len(maps.files) != len(expected) or set(maps.files) != expected:
                    raise RuntimeError('Nuclear helper returned an unexpected set of prediction maps.')
                predictions = []
                for index, image in enumerate(images):
                    values = maps[str(index)]
                    if (values.shape != image.shape[:2] or not np.issubdtype(values.dtype, np.number)
                            or np.iscomplexobj(values) or not np.isfinite(values).all()):
                        raise RuntimeError('Nuclear helper returned invalid prediction-map values or dimensions.')
                    predictions.append(values.copy())
                return predictions
        except (OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile) as error:
            raise RuntimeError('Nuclear helper returned unreadable prediction maps.') from error


def run_prediction(model_path, images_path, maps_path):
    """Headless entry point shared by the source script and frozen executable."""
    from tempo_training import configure_tensorflow, unpack_stardist
    from tempo_nuclei import continuous_map
    configure_tensorflow()
    with tempfile.TemporaryDirectory() as folder, np.load(images_path, allow_pickle=False) as images:
        model = unpack_stardist(model_path, folder)
        maps = {key: continuous_map(model, images[key]) for key in images.files}
        np.savez(maps_path, **maps)
    return 0


def prediction_command(model_path, images_path, maps_path):
    """Return headless failures through a file even without windowed stderr."""
    try:
        return run_prediction(model_path, images_path, maps_path)
    except Exception as error:
        from tempo_credentials import redact_credentials
        failure = dict(error_type=type(error).__name__, message=redact_credentials(str(error))[:2000])
        try:
            Path(str(maps_path)+'.error.json').write_text(json.dumps(failure), encoding='utf-8')
        except OSError:
            pass  # The parent still receives a nonzero exit code.
        return 1


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--predict',nargs=3,metavar=('MODEL','IMAGES','MAPS'),required=True)
    args = parser.parse_args()
    raise SystemExit(prediction_command(*args.predict))
