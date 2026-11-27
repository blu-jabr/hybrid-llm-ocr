# Git Notes

## 1. Pushing a New Branch (First Time)

If you just created a local branch and want to publish it to your remote, you need to set an "upstream" tracking relationship: 

```bash
git push -u origin <branch-name>
```

- **`-`u (or `--set-upstream`)**: Tells Git to link your local branch to the remote branch. You only need to do this the **first time** you push the branch. 
- **origin**: The default name of your remote repository server. 
- **`<branch-name>`:** The name of your local branch. 

## 2. Subsequent Pushes

Once the tracking link is established, you can use a shortcut for all future commits on this branch: 

```bash
git push
```

## 3. Pushing to a Remote Branch with a Different Name

If your local branch is named `master` or `feature-1` and you want to push it to a remote branch named `main` or `production`, use a colon (`:`) to separate them: 

----


