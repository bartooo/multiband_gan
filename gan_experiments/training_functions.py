import copy

import numpy as np
import torch
import wandb
from matplotlib import pyplot as plt
from torch import Tensor
from torch.autograd import Variable
import torch.nn.functional as F

from gan_experiments import gan_utils

torch.autograd.set_detect_anomaly(True)


def train_local_biggan(
    n_epochs,
    task_loader,
    local_generator,
    local_discriminator,
    local_GD,
    num_gen_images,
    task_id,
    n_critic_steps,
    class_cond=False,
    num_classes=None,
):
    # Create batch of latent vectors that we will use to visualize
    # the progression of the generator
    fixed_noise = torch.randn(
        num_gen_images, local_generator.latent_dim, device=local_generator.device
    )

    table_tmp = torch.zeros(num_classes, dtype=torch.long)

    for epoch in range(n_epochs):
        local_generator.train()
        local_discriminator.train()

        for i, batch in enumerate(task_loader):
            if not epoch:
                class_counter = torch.unique(batch[1], return_counts=True)
                table_tmp[class_counter[0]] += class_counter[1].cpu()

            # Configure input
            real_imgs = Variable(batch[0].type(Tensor)).to(local_generator.device)
            if not class_cond:
                task_ids = (torch.zeros([len(batch[0])]) + task_id).to(
                    local_generator.device
                )
            else:
                if len(batch[1]) == 1:
                    task_ids = batch[1].to(local_generator.device)
                else:
                    task_ids = torch.squeeze(batch[1]).to(local_generator.device)

            # ---------------------
            #  Train Discriminator (Critic)
            # ---------------------

            local_discriminator.optim.zero_grad()

            # Sample noise as generator input
            z = Variable(
                Tensor(
                    np.random.normal(
                        0, 1, (real_imgs.shape[0], local_generator.latent_dim)
                    )
                )
            ).to(local_generator.device)

            d_output_fake, d_output_real = local_GD(
                z, task_ids.long(), real_imgs, task_ids.long(), train_G=False
            )

            # Train on real images -> compare predictions to 1
            d_loss_real = torch.mean(F.relu(1.0 - d_output_real))

            # Train on fake images -> compare predictions to -1
            d_loss_fake = torch.mean(F.relu(1.0 + d_output_fake))
            d_loss = d_loss_real + d_loss_fake

            d_loss.backward()
            local_discriminator.optim.step()

            # Train the generator every n_critic steps
            if i % n_critic_steps == 0:
                # -----------------
                #  Train Generator
                # -----------------

                local_generator.optim.zero_grad()

                # Loss measures generator's ability to fool the discriminator
                # Train on fake images -> compare predictions to 1
                d_output_fake = local_GD(z, task_ids.long(), train_G=True)
                g_loss = -torch.mean(d_output_fake)

                g_loss.backward()
                local_generator.optim.step()

                if i % 40 == 0:
                    print(
                        f"[Local D] [Epoch {epoch + 1}/{n_epochs}] [Batch {i + 1}/{len(task_loader)}] [D loss: {d_loss.item():.3f}] [D loss fake: {d_loss_fake.item():.3f}] [D loss real: {d_loss_real.item():.3f}]"
                    )
                    print(
                        f"[Local G] [Epoch {epoch + 1}/{n_epochs}] [Batch {i + 1}/{len(task_loader)}] [G loss: {g_loss.item():.3f}]"
                    )

            wandb.log(
                {
                    f"local_d_loss/task_{task_id}": np.round(d_loss.item(), 3),
                    f"local_d_loss_fake/task_{task_id}": np.round(
                        d_loss_fake.item(), 3
                    ),
                    f"local_d_loss_real/task_{task_id}": np.round(
                        d_loss_real.item(), 3
                    ),
                    f"local_g_loss/task_{task_id}": np.round(g_loss.item(), 3),
                }
            )

        if epoch % 50 == 0:
            local_generator.eval()
            if not class_cond:
                task_ids = (torch.zeros([num_gen_images]) + task_id).to(
                    local_generator.device
                )
            else:
                unique_classes = np.sort(torch.unique(task_ids.cpu()).numpy())
                task_ids = torch.cat(
                    [
                        (torch.zeros([num_gen_images // len(unique_classes)]) + c)
                        for c in unique_classes
                    ]
                ).to(local_generator.device)
                if task_ids.shape[0] < fixed_noise.shape[0]:
                    task_ids = torch.cat(
                        [
                            task_ids,
                            (
                                torch.zeros([fixed_noise.shape[0] - task_ids.shape[0]])
                                + unique_classes[0]
                            ).to(local_generator.device),
                        ]
                    )
            generations = local_generator(
                fixed_noise, local_generator.shared(task_ids.long())
            )
            wandb.log({f"local_generations/task_{task_id}": wandb.Image(generations)})

    return table_tmp


def train_local_wgan_gp(
    n_epochs,
    task_loader,
    local_generator,
    local_discriminator,
    local_gen_lr,
    local_dis_lr,
    num_gen_images,
    task_id,
    lambda_gp,
    n_critic_steps,
    local_scheduler_rate,
    b1,
    b2,
    class_cond=False,
    num_classes=None,
):
    # Optimizers
    optimizer_g = torch.optim.Adam(
        local_generator.parameters(), lr=local_gen_lr, betas=(b1, b2)
    )
    optimizer_d = torch.optim.Adam(
        local_discriminator.parameters(),
        lr=local_dis_lr,
        betas=(b1, b2),
        weight_decay=1e-3,
    )
    # Schedulers
    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
        optimizer_g, gamma=local_scheduler_rate
    )
    scheduler_d = torch.optim.lr_scheduler.ExponentialLR(
        optimizer_d, gamma=local_scheduler_rate
    )

    # Create batch of latent vectors that we will use to visualize
    # the progression of the generator
    fixed_noise = torch.randn(
        num_gen_images, local_generator.latent_dim, device=local_generator.device
    )

    table_tmp = torch.zeros(num_classes, dtype=torch.long)

    for epoch in range(n_epochs):
        local_generator.train()
        for i, batch in enumerate(task_loader):
            if not epoch:
                class_counter = torch.unique(batch[1], return_counts=True)
                table_tmp[class_counter[0]] += class_counter[1].cpu()

            # Configure input
            real_imgs = Variable(batch[0].type(Tensor)).to(local_generator.device)
            if not class_cond:
                task_ids = (torch.zeros([len(batch[0])]) + task_id).to(
                    local_generator.device
                )
            else:
                if len(batch[1]) == 1:
                    task_ids = batch[1].to(local_generator.device)
                else:
                    task_ids = torch.squeeze(batch[1]).to(local_generator.device)

            # ---------------------
            #  Train Discriminator (Critic)
            # ---------------------

            optimizer_d.zero_grad()

            # Sample noise as generator input
            z = Variable(
                Tensor(
                    np.random.normal(
                        0, 1, (real_imgs.shape[0], local_generator.latent_dim)
                    )
                )
            ).to(local_generator.device)

            # Generate a batch of images
            fake_imgs = local_generator(z, task_ids)

            # Add detach() so the backward() will not change the weights of generator
            fake_imgs.detach()

            # Train on real images -> compare predictions to 1
            d_output_real = local_discriminator(
                real_imgs, task_ids if class_cond else None
            )
            d_loss_real = -torch.mean(d_output_real)

            # Train on fake images -> compare predictions to -1
            d_output_fake = local_discriminator(
                fake_imgs, task_ids if class_cond else None
            )
            d_loss_fake = torch.mean(d_output_fake)

            # Gradient penalty
            gradient_penalty = gan_utils.compute_gradient_penalty(
                local_discriminator,
                real_imgs.data,
                fake_imgs.data,
                local_generator.device,
                task_ids=task_ids if class_cond else None,
            )
            # Wasserstein distance
            wasserstein_distance = -(d_loss_real + d_loss_fake)

            # Adversarial loss
            d_loss = d_loss_fake + d_loss_real + lambda_gp * gradient_penalty

            d_loss.backward()
            optimizer_d.step()

            optimizer_g.zero_grad()

            # Train the generator every n_critic steps
            if i % n_critic_steps == 0:
                # -----------------
                #  Train Generator
                # -----------------

                # Generate a batch of images
                fake_imgs = local_generator(z, task_ids)

                # Loss measures generator's ability to fool the discriminator
                # Train on fake images -> compare predictions to 1
                d_output_fake = local_discriminator(
                    fake_imgs, task_ids if class_cond else None
                )
                g_loss = -torch.mean(d_output_fake)

                g_loss.backward()
                optimizer_g.step()

                if i % 40 == 0:
                    print(
                        f"[Local D] [Epoch {epoch + 1}/{n_epochs}] [Batch {i + 1}/{len(task_loader)}] [D loss: {d_loss.item():.3f}] [D loss fake: {d_loss_fake.item():.3f}] [D loss real: {d_loss_real.item():.3f}] [Gradient penalty: {gradient_penalty.item():.3f}] [Wasserstein distance: {wasserstein_distance.item():.3f}]"
                    )
                    print(
                        f"[Local G] [Epoch {epoch + 1}/{n_epochs}] [Batch {i + 1}/{len(task_loader)}] [G loss: {g_loss.item():.3f}]"
                    )

            wandb.log(
                {
                    f"local_d_loss/task_{task_id}": np.round(d_loss.item(), 3),
                    f"local_d_loss_fake/task_{task_id}": np.round(
                        d_loss_fake.item(), 3
                    ),
                    f"local_d_loss_real/task_{task_id}": np.round(
                        d_loss_real.item(), 3
                    ),
                    f"local_g_loss/task_{task_id}": np.round(g_loss.item(), 3),
                    f"local_gradient_penalty/task_{task_id}": np.round(
                        gradient_penalty.item(), 3
                    ),
                    f"local_wasserstein_distance/task_{task_id}": np.round(
                        wasserstein_distance.item(), 3
                    ),
                }
            )

        if epoch % 50 == 0:
            local_generator.eval()
            if not class_cond:
                task_ids = (torch.zeros([num_gen_images]) + task_id).to(
                    local_generator.device
                )
            else:
                unique_classes = np.sort(torch.unique(task_ids.cpu()).numpy())
                task_ids = torch.cat(
                    [
                        (torch.zeros([num_gen_images // len(unique_classes)]) + c)
                        for c in unique_classes
                    ]
                ).to(local_generator.device)
                if task_ids.shape[0] < fixed_noise.shape[0]:
                    task_ids = torch.cat(
                        [
                            task_ids,
                            (
                                torch.zeros([fixed_noise.shape[0] - task_ids.shape[0]])
                                + unique_classes[0]
                            ).to(local_generator.device),
                        ]
                    )
            generations = local_generator(fixed_noise, task_ids)
            wandb.log({f"local_generations/task_{task_id}": wandb.Image(generations)})

        scheduler_g.step()
        scheduler_d.step()

    return table_tmp


def train_local(
    local_generator,
    local_discriminator,
    n_epochs,
    task_loader,
    task_id,
    local_dis_lr,
    local_gen_lr,
    num_gen_images,
    local_scheduler_rate,
    n_critic_steps,
    lambda_gp,
    b1,
    b2,
    class_cond=False,
    num_classes=None,
    local_GD=None,
):
    local_generator.train()
    local_discriminator.train()
    local_generator.translator.train()

    if local_GD is not None:
        return train_local_biggan(
            n_epochs,
            task_loader,
            local_generator,
            local_discriminator,
            local_GD,
            num_gen_images,
            task_id,
            n_critic_steps,
            class_cond,
            num_classes,
        )
    else:
        return train_local_wgan_gp(
            n_epochs,
            task_loader,
            local_generator,
            local_discriminator,
            local_gen_lr,
            local_dis_lr,
            num_gen_images,
            task_id,
            lambda_gp,
            n_critic_steps,
            local_scheduler_rate,
            b1,
            b2,
            class_cond,
            num_classes,
        )


def train_global_generator(
    batch_size,
    task_id,
    limit_previous_examples,
    curr_global_generator,
    n_epochs,
    task_loader,
    curr_local_generator,
    global_gen_lr,
    warmup_rounds,
    num_epochs_noise_optim,
    num_gen_images,
    optim_noise_lr,
    global_scheduler_rate,
    class_cond=False,
    class_table=None,
    biggan_training=False,
    only_generations=False,
):
    global_generator = copy.deepcopy(curr_global_generator)
    global_generator.to(curr_global_generator.device)
    curr_local_generator.eval()
    curr_global_generator.eval()
    curr_global_generator.translator.eval()

    criterion = torch.nn.MSELoss()

    optimizer_g = torch.optim.Adam(
        global_generator.translator.parameters(), lr=global_gen_lr
    )  # at the beginning train only translator
    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
        optimizer_g, gamma=global_scheduler_rate
    )

    # Create batch of latent vectors that we will use to visualize
    # the progression of the generator
    fixed_noise = torch.randn(
        num_gen_images, global_generator.latent_dim, device=global_generator.device
    )

    # How many examples from all prev tasks we want to generate
    n_prev_examples = int(batch_size * min(task_id, 3) * limit_previous_examples)

    curr_noise_all = []

    for epoch in range(n_epochs):
        global_generator.train()
        if epoch == warmup_rounds:
            print("End of warmup")
            optimizer_g = torch.optim.Adam(
                global_generator.parameters(), lr=global_gen_lr
            )
            scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
                optimizer_g, gamma=global_scheduler_rate
            )

        for i, batch in enumerate(task_loader):
            # Generate data -> (noise, generation) pairs for each previous task
            prev_examples, prev_noise, prev_task_ids = gan_utils.generate_previous_data(
                n_prev_tasks=task_id,
                n_prev_examples=n_prev_examples,
                curr_global_generator=curr_global_generator,
                class_table=class_table,
                biggan_training=biggan_training,
            )
            curr_labels = batch[1] if class_cond else None
            if not class_cond:
                curr_task_ids = torch.zeros([len(batch[0])]) + task_id
            else:
                curr_task_ids = curr_labels
            if not only_generations:
                # Real images and optimized noise of current task
                curr_examples = batch[0]
                if not epoch:
                    curr_noise = gan_utils.optimize_noise(
                        curr_examples,
                        curr_local_generator,
                        num_epochs_noise_optim,
                        task_id,
                        lr=optim_noise_lr,
                        log=not i,  # log only first batch for readability
                        labels=curr_labels,
                        biggan_training=biggan_training,
                    )
                    curr_noise_all.append(curr_noise.to("cpu"))
                else:
                    curr_noise = curr_noise_all[i].to(global_generator.device)
            else:
                curr_noise = torch.randn(
                    len(batch[0]),
                    curr_local_generator.latent_dim,
                    device=curr_local_generator.device,
                )
                curr_examples = curr_local_generator(curr_noise, curr_task_ids)

            examples_concat = torch.cat(
                [prev_examples, curr_examples.to(global_generator.device)]
            )
            noise_concat = torch.cat([prev_noise, curr_noise])
            task_ids_concat = torch.cat(
                [prev_task_ids, curr_task_ids.to(global_generator.device)]
            )

            # Randomly shuffle examples
            shuffle = torch.randperm(len(task_ids_concat))
            examples_concat = examples_concat[shuffle]
            noise_concat = noise_concat[shuffle]
            task_ids_concat = task_ids_concat[shuffle]

            optimizer_g.zero_grad()

            if biggan_training:
                task_ids_concat = global_generator.shared(task_ids_concat.long())
            global_generations = global_generator(noise_concat, task_ids_concat)
            g_loss = criterion(global_generations, examples_concat)

            g_loss.backward()
            optimizer_g.step()

            if i % 20 == 0 or not epoch:
                print(
                    f"[Global G] [Epoch {epoch + 1}/{n_epochs}] [Batch {i + 1}/{len(task_loader)}] [G loss: {g_loss.item():.3f}]"
                )

            if n_prev_examples:
                wandb.log(
                    {
                        # f"prev_examples_task_{task_id}": wandb.Image(prev_examples),
                        # f"global_generations_task_{task_id}": wandb.Image(
                        #     global_generations
                        # ),
                        f"global_g_loss/task_{task_id}": np.round(g_loss.item(), 3),
                    }
                )
            else:
                wandb.log(
                    {
                        # f"global_generations_task_{task_id}": wandb.Image(
                        #     global_generations
                        # ),
                        f"global_g_loss/task_{task_id}": np.round(g_loss.item(), 3),
                    }
                )

        scheduler_g.step()

        if epoch % 10 == 0:
            global_generator.eval()
            for learned_task_id in range(0, task_id + 1):
                if not class_cond:
                    task_ids = (torch.zeros([num_gen_images]) + learned_task_id).to(
                        global_generator.device
                    )
                else:
                    n_classes_per_task = torch.unique(curr_labels).shape[0]
                    classes_to_generate = [
                        c
                        for c in range(
                            learned_task_id * 2,
                            (learned_task_id * 2) + n_classes_per_task,
                        )
                    ]
                    task_ids = torch.cat(
                        [
                            (
                                torch.zeros(
                                    [fixed_noise.shape[0] // len(classes_to_generate)]
                                )
                                + c
                            )
                            for c in classes_to_generate
                        ]
                    ).to(global_generator.device)
                    if task_ids.shape[0] < fixed_noise.shape[0]:
                        task_ids = torch.cat(
                            [
                                task_ids,
                                (
                                    torch.zeros(
                                        [fixed_noise.shape[0] - task_ids.shape[0]]
                                    )
                                    + classes_to_generate[0]
                                ).to(global_generator.device),
                            ]
                        )

                if biggan_training:
                    task_ids = global_generator.shared(task_ids.long())
                generations = global_generator(
                    fixed_noise,
                    task_ids,
                )
                wandb.log(
                    {
                        f"global_generations/task_{task_id}_of_task_{learned_task_id}": wandb.Image(
                            generations
                        )
                    }
                )

    return global_generator
