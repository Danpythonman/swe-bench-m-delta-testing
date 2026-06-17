from sbmdt.alibaba import AlibabaEvaluator

__all__ = ['evaluate']


def evaluate(instance_id: str):
    if instance_id.startswith('alibaba'):
        evaluator = AlibabaEvaluator(instance_id=instance_id)
    else:
        raise Exception(f'unknown instance ID {instance_id}')
    evaluator.run()
